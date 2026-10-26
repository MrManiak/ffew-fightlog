"""Small fight helpers shared by ingest, clustering, curation and tasks."""

from django.db import transaction
from django.db.models import F

from fightlog.models import Fight, FightChange


def bump_versions(fight_ids) -> None:
    ids = [i for i in fight_ids if i]
    if ids:
        Fight.objects.filter(pk__in=ids).update(data_version=F("data_version") + 1)


def queue_rebuilds(fight_ids, countdown: int = 15) -> None:
    from fightlog.tasks import rebuild_fight

    ids = sorted({i for i in fight_ids if i})

    def send():
        for fight_id in ids:
            rebuild_fight.apply_async(args=[fight_id], countdown=countdown, priority=5)

    transaction.on_commit(send)


def record_change(fight: Fight, user, action: str, curates: bool = True, **details) -> FightChange:
    """History entry. Any human edit makes the fight curated (no auto-merging)."""
    change = FightChange.objects.create(fight=fight, user=user, action=action, details=details)
    if curates and user is not None and not fight.is_curated:
        fight.is_curated = True
        Fight.objects.filter(pk=fight.pk).update(is_curated=True)
    return change


def can_edit(user, fight: Fight) -> bool:
    if not user.is_authenticated or not user.has_perm("fightlog.basic_access"):
        return False
    if user.has_perm("fightlog.curate_fights"):
        return True
    return fight.engagements.filter(log__uploads__user=user).exists()


def is_kept_when_empty(fight: Fight) -> bool:
    return (
        fight.is_curated
        or fight.title_is_manual
        or fight.discord_posts.exists()
        or bool(fight.notes)
        or fight.tags.exists()
    )
