"""Celery tasks (technical plan §4.2)."""

from datetime import timedelta

import requests
from celery import shared_task

from django.utils import timezone

from allianceauth.notifications import notify
from allianceauth.services.hooks import get_extension_logger
from allianceauth.services.tasks import QueueOnce

from fightlog import app_settings
from fightlog.locks import LockBusy, cache_lock
from fightlog.models import CombatLog, DiscordPost, Fight
from fightlog.parser import PARSER_VERSION

logger = get_extension_logger(__name__)

RECHECKS = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))


def _notify_uploaders(log: CombatLog, title: str, message: str, level: str = "danger") -> None:
    for upload in log.uploads.select_related("user"):
        notify(upload.user, title, message, level)


@shared_task(base=QueueOnce, once={"keys": ["log_id"], "graceful": True})
def process_log(log_id: int) -> None:
    from fightlog.services import ingest
    from fightlog.services.fights import queue_rebuilds

    log = CombatLog.objects.filter(pk=log_id).first()
    if log is None:
        return
    try:
        affected = ingest.process_log(log)
    except Exception as ex:  # noqa: BLE001 - report every failure to the uploader
        logger.exception("Processing log %s failed", log_id)
        CombatLog.objects.filter(pk=log_id).update(status=CombatLog.Status.FAILED, status_message=str(ex)[:255])
        _notify_uploaders(log, "Fight log processing failed", f"{log.listener_name}: {ex}")
        return
    if log.status in (CombatLog.Status.REJECTED, CombatLog.Status.FAILED):
        _notify_uploaders(log, "Fight log rejected", f"{log.listener_name}: {log.status_message}")
    queue_rebuilds(affected)


@shared_task(
    bind=True,
    base=QueueOnce,
    once={"keys": ["fight_id"], "graceful": True, "unlock_before_run": True},
    max_retries=None,
)
def rebuild_fight(self, fight_id: int) -> None:
    """Rebuild one fight. Uploads that land during a run queue exactly one follow-up."""
    from fightlog.services.rebuild import rebuild_fight as do_rebuild

    try:
        with cache_lock(f"rebuild-{fight_id}", timeout=900, blocking=False):
            fight = do_rebuild(fight_id)
    except LockBusy as ex:
        raise self.retry(countdown=10) from ex
    if fight is not None and app_settings.FIGHTLOG_ZKB_ENABLED and fight.zkb_checked_at is None:
        link_zkill.apply_async(args=[fight_id, 0], countdown=5, priority=6)


@shared_task(bind=True, base=QueueOnce, once={"keys": ["fight_id", "recheck"], "graceful": True}, max_retries=5)
def link_zkill(self, fight_id: int, recheck: int = 0) -> None:
    from fightlog.services import zkb
    from fightlog.services.fights import bump_versions, queue_rebuilds

    if not app_settings.FIGHTLOG_ZKB_ENABLED:
        return
    fight = Fight.objects.filter(pk=fight_id).first()
    if fight is None:
        return
    system_before = (fight.system_id, fight.system_source)
    try:
        new_links = zkb.link_fight(fight)
    except zkb.ZkbRateLimited as ex:
        raise self.retry(countdown=max(1, int(ex.retry_at - timezone.now().timestamp()) + 1))
    except requests.RequestException as ex:
        raise self.retry(countdown=300, exc=ex)
    fight.refresh_from_db()
    if new_links or (fight.system_id, fight.system_source) != system_before:
        bump_versions([fight_id])
        queue_rebuilds([fight_id], countdown=2)
    now = timezone.now()
    if recheck < len(RECHECKS) and now - fight.end_at < timedelta(hours=24):
        eta = fight.end_at + RECHECKS[recheck]
        if eta > now:
            link_zkill.apply_async(args=[fight_id, recheck + 1], eta=eta, priority=6)


@shared_task(bind=True, base=QueueOnce, once={"keys": ["post_id"], "graceful": True}, max_retries=5)
def post_fight_to_discord(self, post_id: int) -> None:
    from fightlog.services import discord

    post = DiscordPost.objects.select_related("fight", "webhook", "user").filter(pk=post_id).first()
    if post is None:
        return
    try:
        discord.send(post)
    except discord.DiscordRateLimited as ex:
        raise self.retry(countdown=int(ex.retry_after) + 1)
    except requests.RequestException as ex:
        post.status, post.error = DiscordPost.Status.FAILED, str(ex)[:500]
        post.save(update_fields=["status", "error"])
    if post.user:
        if post.status == DiscordPost.Status.SENT:
            notify(post.user, "Fight posted to Discord", f"{post.fight.title} → {post.webhook.name}", "success")
        else:
            notify(post.user, "Discord post failed", f"{post.fight.title}: {post.error}", "danger")


@shared_task(base=QueueOnce)
def reparse_all() -> int:
    """Re-parse logs written by an older parser, including no-PvP logs still kept."""
    stale = CombatLog.objects.filter(parser_version__lt=PARSER_VERSION).exclude(status=CombatLog.Status.REJECTED)
    count = 0
    for log_id in stale.values_list("pk", flat=True):
        process_log.apply_async(args=[log_id], priority=8)
        count += 1
    return count


@shared_task(base=QueueOnce)
def purge_expired_logs() -> int:
    _, per_model = CombatLog.objects.filter(status=CombatLog.Status.NO_PVP, purge_after__lt=timezone.now()).delete()
    return per_model.get("fightlog.CombatLog", 0)
