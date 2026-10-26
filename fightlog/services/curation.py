"""Curation actions with history (technical plan §4.4 and §8)."""

from datetime import datetime
from datetime import timezone as dt_timezone

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from fightlog import app_settings
from fightlog.analysis.attribution import parse_ship_only_key
from fightlog.analysis.store import EventStore
from fightlog.models import (
    AttributionOverride,
    CombatLog,
    DiscordPost,
    Engagement,
    Fight,
    FightChange,
    FightKillmail,
    FightRedirect,
    Side,
    SideOverride,
    Tag,
)

from .fights import bump_versions, queue_rebuilds, record_change


def _side_for_order(fight: Fight, order: int, name: str | None = None) -> Side:
    side = fight.sides.filter(order=order).first()
    if side is None:
        default = "Friendly" if order == 0 else ("Hostile" if order == 1 else f"Hostile {order}")
        side = Side.objects.create(fight=fight, order=order, is_friendly=order == 0, name=name or default)
    return side


def merge_fights(fights: list[Fight], user=None) -> Fight:
    """Merge into one fight: the curated one, else the one with most logs, else the oldest."""
    fights = list({f.pk: f for f in fights}.values())
    counts = dict(
        Fight.objects.filter(pk__in=[f.pk for f in fights])
        .annotate(n=Count("engagements__log", distinct=True))
        .values_list("pk", "n")
    )
    survivor = sorted(fights, key=lambda f: (not f.is_curated, -counts.get(f.pk, 0), f.pk))[0]
    absorbed = []
    with transaction.atomic():
        for other in fights:
            if other.pk == survivor.pk:
                continue
            Engagement.objects.filter(fight=other).update(fight=survivor)
            if other.title_is_manual and not survivor.title_is_manual:
                survivor.title, survivor.title_is_manual = other.title, True
            if other.notes:
                survivor.notes = (survivor.notes + "\n\n" if survivor.notes else "") + (
                    f"— merged from fight #{other.pk} —\n{other.notes}"
                )
            survivor.tags.add(*other.tags.all())
            if other.system_id and (not survivor.system_id or other.system_source == "manual"):
                survivor.system_id, survivor.system_source = other.system_id, other.system_source
            survivor.start_at = min(survivor.start_at, other.start_at)
            survivor.end_at = max(survivor.end_at, other.end_at)
            survivor.is_curated = survivor.is_curated or other.is_curated
            FightChange.objects.filter(fight=other).update(fight=survivor)
            DiscordPost.objects.filter(fight=other).update(fight=survivor)
            taken = set(survivor.side_overrides.values_list("participant_key", flat=True))
            for override in SideOverride.objects.filter(fight=other).select_related("side"):
                if override.participant_key not in taken:
                    target = (
                        _side_for_order(survivor, override.side.order, override.side.name) if override.side else None
                    )
                    SideOverride.objects.create(
                        fight=survivor, participant_key=override.participant_key, side=target, user=override.user
                    )
            taken = set(survivor.attribution_overrides.values_list("log_id", "polarity", "raw_label"))
            for override in AttributionOverride.objects.filter(fight=other):
                if (override.log_id, override.polarity, override.raw_label) not in taken:
                    override.pk = None
                    override.fight = survivor
                    override.save()
            existing = set(survivor.fight_killmails.values_list("killmail_id", flat=True))
            for link in FightKillmail.objects.filter(fight=other):
                if link.killmail_id not in existing:
                    FightKillmail.objects.create(fight=survivor, killmail_id=link.killmail_id, role=link.role)
            FightRedirect.objects.filter(fight=other).update(fight=survivor)
            FightRedirect.objects.update_or_create(old_id=other.pk, defaults={"fight": survivor})
            absorbed.append(other.pk)
            other.delete()
        survivor.save()
        record_change(survivor, user, FightChange.Action.MERGE, absorbed=absorbed)
        bump_versions([survivor.pk])
    queue_rebuilds([survivor.pk], countdown=2)
    return survivor


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def split_fight(fight: Fight, at: datetime, user) -> Fight:
    """Engagements after ``at`` move to a new fight; engagements spanning it are cut in two."""
    cut = _epoch(at)
    with transaction.atomic():
        new = Fight.objects.create(title=f"Fight {at:%Y-%m-%d %H:%M}", start_at=at, end_at=at, is_curated=True)
        for engagement in list(fight.engagements.all()):
            if engagement.start_at >= at:
                engagement.fight = new
                engagement.save(update_fields=["fight"])
            elif engagement.end_at > at:
                store = EventStore.decode(engagement.store)
                times = store.column("t")
                idx = next((i for i, t in enumerate(times) if t >= cut), len(times))
                before, after = store.slice(0, idx), store.slice(idx, len(times))
                if len(after):
                    engagement.store = before.encode(app_settings.FIGHTLOG_MAX_BLOB_BYTES)
                    engagement.end_at = datetime.fromtimestamp(before.column("t")[-1], tz=dt_timezone.utc)
                    engagement.event_count = len(before)
                    engagement.save()
                    Engagement.objects.create(
                        log=engagement.log,
                        fight=new,
                        start_at=datetime.fromtimestamp(after.column("t")[0], tz=dt_timezone.utc),
                        end_at=datetime.fromtimestamp(after.column("t")[-1], tz=dt_timezone.utc),
                        event_count=len(after),
                        actor_keys=engagement.actor_keys,
                        store=after.encode(app_settings.FIGHTLOG_MAX_BLOB_BYTES),
                    )
        from .clustering import refresh_window

        refresh_window(fight)
        refresh_window(new)
        record_change(fight, user, FightChange.Action.SPLIT, at=at.isoformat(), new_fight=new.pk)
        record_change(new, user, FightChange.Action.SPLIT, at=at.isoformat(), from_fight=fight.pk)
        Fight.objects.filter(pk__in=[fight.pk, new.pk]).update(is_curated=True)
        bump_versions([fight.pk, new.pk])
    queue_rebuilds([fight.pk, new.pk], countdown=2)
    return new


def move_engagement(engagement: Engagement, target: Fight, user) -> None:
    source = engagement.fight
    with transaction.atomic():
        engagement.fight = target
        engagement.save(update_fields=["fight"])
        from .clustering import refresh_window

        for fight in (source, target):
            if fight:
                refresh_window(fight)
                record_change(
                    fight,
                    user,
                    FightChange.Action.MOVE,
                    engagement=engagement.pk,
                    source=source.pk if source else None,
                    target=target.pk,
                )
        bump_versions([f.pk for f in (source, target) if f])
    queue_rebuilds([f.pk for f in (source, target) if f], countdown=2)


def rename(fight: Fight, title: str, user) -> None:
    fight.title, fight.title_is_manual = title[:200], True
    fight.save(update_fields=["title", "title_is_manual"])
    record_change(fight, user, FightChange.Action.RENAME, title=fight.title)


def set_notes(fight: Fight, notes: str, user) -> None:
    fight.notes = notes
    fight.save(update_fields=["notes"])
    record_change(fight, user, FightChange.Action.NOTES)


def set_tags(fight: Fight, tags: list[str], user) -> None:
    clean = sorted({t.strip().lstrip("#")[:50] for t in tags if t.strip().lstrip("#")})
    fight.tags.set([Tag.objects.get_or_create(name=t)[0] for t in clean])
    record_change(fight, user, FightChange.Action.TAGS, tags=clean)


def set_system(fight: Fight, system_name: str, user) -> bool:
    from .resolve import solar_system_by_name

    system = solar_system_by_name(system_name)
    if system is None:
        return False
    fight.system, fight.system_source = system, Fight.SystemSource.MANUAL
    fight.save(update_fields=["system", "system_source"])
    record_change(fight, user, FightChange.Action.SYSTEM, system=system.name)
    return True


def set_side(fight: Fight, keys: list[str], side: Side | None, user) -> None:
    with transaction.atomic():
        for key in keys:
            SideOverride.objects.update_or_create(
                fight=fight, participant_key=key, defaults={"side": side, "user": user}
            )
        record_change(fight, user, FightChange.Action.SIDE, keys=keys, side=side.name if side else "Unknown")
        bump_versions([fight.pk])
    queue_rebuilds([fight.pk], countdown=1)


def rename_side(side: Side, name: str, user) -> None:
    side.name, side.name_is_manual = name[:100], True
    side.save(update_fields=["name", "name_is_manual"])
    record_change(side.fight, user, FightChange.Action.SIDE_RENAME, side=side.pk, name=side.name)


def attribute(fight: Fight, actor_key: str, pilot_key: str, user) -> bool:
    parsed = parse_ship_only_key(actor_key)
    if not parsed:
        return False
    log_id, polarity, label = parsed
    with transaction.atomic():
        AttributionOverride.objects.update_or_create(
            fight=fight,
            log_id=log_id,
            polarity=polarity,
            raw_label=label,
            defaults={"participant_key": pilot_key, "user": user},
        )
        record_change(fight, user, FightChange.Action.ATTRIBUTE, actor=actor_key, pilot=pilot_key)
        bump_versions([fight.pk])
    queue_rebuilds([fight.pk], countdown=1)
    return True


def set_excluded(log: CombatLog, excluded: bool, reason: str, user) -> list[int]:
    log.excluded = excluded
    log.excluded_reason = reason[:255] if excluded else ""
    log.excluded_by = user if excluded else None
    log.excluded_at = timezone.now() if excluded else None
    log.save(update_fields=["excluded", "excluded_reason", "excluded_by", "excluded_at"])
    fight_ids = list(Fight.objects.filter(engagements__log=log).distinct().values_list("pk", flat=True))
    for fight in Fight.objects.filter(pk__in=fight_ids):
        action = FightChange.Action.EXCLUDE if excluded else FightChange.Action.INCLUDE
        record_change(fight, user, action, log=log.pk, listener=log.listener_name, reason=log.excluded_reason)
    bump_versions(fight_ids)
    queue_rebuilds(fight_ids, countdown=1)
    return fight_ids


def can_delete_log(user, log: CombatLog) -> bool:
    if user.has_perm("fightlog.curate_fights"):
        return True
    if log.uploads.filter(user=user).exists():
        return True
    return (
        bool(log.listener_character_id)
        and user.character_ownerships.filter(character__character_id=log.listener_character_id).exists()
    )


def delete_log(log: CombatLog, user) -> list[int]:
    fight_ids = list(Fight.objects.filter(engagements__log=log).distinct().values_list("pk", flat=True))
    with transaction.atomic():
        for fight in Fight.objects.filter(pk__in=fight_ids):
            record_change(
                fight, user, FightChange.Action.DELETE_LOG, curates=False, log=log.pk, listener=log.listener_name
            )
        log.delete()
        bump_versions(fight_ids)
    queue_rebuilds(fight_ids, countdown=1)
    return fight_ids
