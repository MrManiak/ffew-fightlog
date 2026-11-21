"""Engagement → fight assignment and automatic merging (technical plan §4.4)."""

from datetime import timedelta

from django.db import transaction

from fightlog import app_settings
from fightlog.locks import cache_lock
from fightlog.models import CombatLog, Engagement, Fight

from .fights import bump_versions


def _fight_keys(fight: Fight) -> set[str]:
    keys: set[str] = set()
    for actor_keys in fight.engagements.values_list("actor_keys", flat=True):
        keys.update(actor_keys or [])
    return keys


def _overlap(fight: Fight, engagement) -> float:
    """Seconds a fight and an engagement share (negative when they don't touch)."""
    lo = max(fight.start_at, engagement.start_at)
    hi = min(fight.end_at, engagement.end_at)
    return (hi - lo).total_seconds()


def _qualifying(fights: list[Fight], engagement, keys: set[str], listener_key: str) -> list[tuple[int, Fight]]:
    """(shared pilot count, fight) for each fight the engagement belongs to."""
    qualifying = []
    for fight in fights:
        fight_keys = _fight_keys(fight)
        shared = len(keys & fight_keys)
        if shared or listener_key in fight_keys:
            qualifying.append((shared, fight))
    if not qualifying and fights and not keys - {listener_key}:
        # An observer log (only third-party tackle, no named pilots): join the
        # overlapping fight it shares the most time with.
        qualifying = [(0, max(fights, key=lambda f: _overlap(f, engagement)))]
    return qualifying


def assign_log_engagements(log: CombatLog) -> set[int]:
    """Put each unassigned engagement of a log into a fight. Returns affected fight IDs."""
    tolerance = timedelta(seconds=app_settings.FIGHTLOG_MERGE_TOLERANCE_SECONDS)
    affected: set[int] = set()
    with cache_lock("clustering", timeout=300):
        with transaction.atomic():
            for engagement in log.engagements.filter(fight__isnull=True):
                keys = set(engagement.actor_keys or [])
                listener_key = f"p:{log.listener_name}"
                nearby = list(  # already within the merge tolerance
                    Fight.objects.filter(
                        start_at__lte=engagement.end_at + tolerance, end_at__gte=engagement.start_at - tolerance
                    )
                )
                qualifying = _qualifying([f for f in nearby if not f.is_deleted], engagement, keys, listener_key)
                if not qualifying:
                    # A late log of a deleted fight joins it: it stays hidden and shares its fate.
                    # Deleted fights are never merged, and never win over a visible fight.
                    late = _qualifying([f for f in nearby if f.is_deleted], engagement, keys, listener_key)
                    if late:
                        qualifying = [max(late, key=lambda sf: (sf[0], -sf[1].pk))]
                if not qualifying:
                    fight = Fight.objects.create(
                        title=f"Fight {engagement.start_at:%Y-%m-%d %H:%M}",
                        start_at=engagement.start_at,
                        end_at=engagement.end_at,
                    )
                elif len(qualifying) == 1:
                    fight = qualifying[0][1]
                else:
                    curated = [(s, f) for s, f in qualifying if f.is_curated]
                    if curated:
                        fight = max(curated, key=lambda sf: (sf[0], -sf[1].pk))[1]
                    else:
                        from .curation import merge_fights

                        fights = [f for _, f in qualifying]
                        fight = merge_fights(fights, user=None)
                engagement.fight = fight
                engagement.save(update_fields=["fight"])
                changed = False
                if engagement.start_at < fight.start_at:
                    fight.start_at = engagement.start_at
                    changed = True
                if engagement.end_at > fight.end_at:
                    fight.end_at = engagement.end_at
                    changed = True
                if changed:
                    fight.save(update_fields=["start_at", "end_at"])
                affected.add(fight.pk)
            bump_versions(affected)
    return affected


def refresh_window(fight: Fight) -> None:
    span = fight.engagements.order_by("start_at").values_list("start_at", "end_at")
    rows = list(span)
    if rows:
        fight.start_at = min(r[0] for r in rows)
        fight.end_at = max(r[1] for r in rows)
        fight.save(update_fields=["start_at", "end_at"])


__all__ = ["Engagement", "assign_log_engagements", "refresh_window"]
