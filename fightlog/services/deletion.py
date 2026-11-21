"""Deleting fights: mark as deleted, restore, and delete for good once the retention is over.

A deleted fight keeps all its data and stays hidden from everyone but curators. Until it is
purged it can be restored as it was. The purge deletes the fight and the logs only it uses.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from allianceauth.services.hooks import get_extension_logger

from fightlog import app_settings
from fightlog.locks import LockBusy, cache_lock
from fightlog.models import CombatLog, Engagement, Fight, FightChange

from .fights import record_change

logger = get_extension_logger(__name__)


@dataclass
class PurgeResult:
    fights: int = 0
    logs_deleted: int = 0
    logs_trimmed: int = 0

    def add(self, other: "PurgeResult") -> None:
        self.fights += other.fights
        self.logs_deleted += other.logs_deleted
        self.logs_trimmed += other.logs_trimmed


def retention_label() -> str:
    """How long a deleted fight can be restored, for messages: "7 days"."""
    days = app_settings.FIGHTLOG_DELETED_FIGHT_RETENTION_DAYS
    return f"{days} day" + ("" if days == 1 else "s")


def delete_fight(fight: Fight, user, reason: str = "") -> bool:
    """Mark a fight as deleted. False when it already was."""
    now = timezone.now()
    state = {
        "deleted_at": now,
        "deleted_by": user,
        "deleted_reason": reason.strip()[:255],
        "purge_after": now + timedelta(days=app_settings.FIGHTLOG_DELETED_FIGHT_RETENTION_DAYS),
    }
    with transaction.atomic():
        # update(), not save(): a rebuild running right now must not write a stale copy back.
        if not Fight.objects.filter(pk=fight.pk, deleted_at__isnull=True).update(**state):
            return False
        record_change(fight, user, FightChange.Action.DELETE, curates=False, reason=state["deleted_reason"])
    for name, value in state.items():
        setattr(fight, name, value)
    return True


def restore_fight(fight: Fight, user) -> bool:
    """Bring a deleted fight back as it was. False when it wasn't deleted."""
    state = {"deleted_at": None, "deleted_by": None, "deleted_reason": "", "purge_after": None}
    with transaction.atomic():
        if not Fight.objects.filter(pk=fight.pk, deleted_at__isnull=False).update(**state):
            return False
        record_change(fight, user, FightChange.Action.RESTORE, curates=False)
        if app_settings.FIGHTLOG_ZKB_ENABLED:
            # zKillboard checks are skipped while a fight is deleted.
            from fightlog.tasks import link_zkill

            transaction.on_commit(lambda: link_zkill.apply_async(args=[fight.pk, 0], countdown=5, priority=6))
    for name, value in state.items():
        setattr(fight, name, value)
    return True


def merge_ranges(ranges) -> list[list[int]]:
    """Sorted [start, end] pairs with overlapping ones joined."""
    merged: list[list[int]] = []
    for start, end in sorted((int(lo), int(hi)) for lo, hi in ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def purge_fight(fight: Fight, user=None) -> PurgeResult | None:
    """Delete a deleted fight for good, with every log that only it uses.

    A log another fight also uses is kept. It loses this fight's engagements, and their time
    ranges are remembered on the log so that a re-parse can't bring the fight back.

    Returns None when the fight isn't marked as deleted (any more). Raises LockBusy while the
    fight is being rebuilt or logs are being assigned to fights.
    """
    result = PurgeResult(fights=1)
    with cache_lock(f"rebuild-{fight.pk}", timeout=900, blocking=False), cache_lock("clustering", timeout=300):
        with transaction.atomic():
            fight = Fight.objects.select_for_update().filter(pk=fight.pk, deleted_at__isnull=False).first()
            if fight is None:
                return None
            spans: dict[int, list[list[int]]] = defaultdict(list)
            for log_id, start_at, end_at in fight.engagements.values_list("log_id", "start_at", "end_at"):
                spans[log_id].append([int(start_at.timestamp()), int(end_at.timestamp())])
            # Engagements without a fight count as another use too: the log stays.
            shared = set(
                Engagement.objects.filter(log_id__in=list(spans)).exclude(fight=fight).values_list("log_id", flat=True)
            )
            for log in CombatLog.objects.filter(pk__in=shared).only("pk", "purged_ranges"):
                log.purged_ranges = merge_ranges([*(log.purged_ranges or []), *spans[log.pk]])
                log.save(update_fields=["purged_ranges"])
            fight.engagements.all().delete()
            _, per_model = CombatLog.objects.filter(pk__in=set(spans) - shared).delete()
            result.logs_deleted = per_model.get("fightlog.CombatLog", 0)
            result.logs_trimmed = len(shared)
            fight_id, title = fight.pk, fight.title
            fight.delete()
    logger.info(
        "Purged fight %s (%s)%s: %d logs deleted, %d shared logs trimmed",
        fight_id,
        title,
        f" for {user}" if user else "",
        result.logs_deleted,
        result.logs_trimmed,
    )
    return result


def purge_expired_fights(now: datetime | None = None) -> PurgeResult:
    """Purge every deleted fight whose retention is over. Busy fights wait for the next run."""
    total = PurgeResult()
    expired = Fight.objects.filter(deleted_at__isnull=False, purge_after__lte=now or timezone.now())
    for fight in expired.order_by("pk"):
        try:
            result = purge_fight(fight)
        except LockBusy:
            logger.warning("Fight %s is busy; it will be purged on the next run", fight.pk)
            continue
        if result:
            total.add(result)
    return total
