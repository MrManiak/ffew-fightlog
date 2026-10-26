"""Celery tasks and management commands."""

from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from celery.exceptions import Retry

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from fightlog import tasks
from fightlog.locks import LockBusy, cache_lock
from fightlog.models import CombatLog, DiscordPost, DiscordWebhook, Fight
from fightlog.services import ingest, zkb

from .utils import FIVE_LOGS, SAMPLE_LOG, fixture_bytes, fixture_filename, fleet_owner, ingest_fixture, offline


class TaskTest(TestCase):
    def setUp(self):
        self.user = fleet_owner()

    def test_process_log_task(self):
        log = ingest.accept_upload(self.user, fixture_filename(SAMPLE_LOG), fixture_bytes(SAMPLE_LOG)).log
        with offline(), self.captureOnCommitCallbacks(execute=False) as callbacks:
            tasks.process_log(log.pk)
        log.refresh_from_db()
        self.assertEqual(log.status, CombatLog.Status.PARSED)
        self.assertTrue(callbacks)  # the rebuild is queued after commit
        tasks.process_log(999999)

    def test_process_log_failure_notifies(self):
        log = ingest.accept_upload(self.user, fixture_filename(SAMPLE_LOG), fixture_bytes(SAMPLE_LOG)).log
        with (
            mock.patch("fightlog.services.ingest.process_log", side_effect=RuntimeError("boom")),
            mock.patch("fightlog.tasks.notify") as notify,
        ):
            tasks.process_log(log.pk)
        log.refresh_from_db()
        self.assertEqual(log.status, CombatLog.Status.FAILED)
        notify.assert_called()

    def test_rejected_log_notifies(self):
        from .test_parser import log_text

        german = log_text(*["{} Schaden an X".format(i) for i in range(30)]).replace(b"Pilot Listener", b"Pilot 001")
        log = ingest.accept_upload(self.user, "20261025_195145_90000001.txt", german).log
        with mock.patch("fightlog.tasks.notify") as notify:
            tasks.process_log(log.pk)
        log.refresh_from_db()
        self.assertEqual(log.status, CombatLog.Status.REJECTED)
        notify.assert_called()

    def test_rebuild_task_queues_zkill_and_retries_when_busy(self):
        with offline():
            ingest_fixture(self.user, [SAMPLE_LOG], process=True)
        fight = Fight.objects.get()
        Fight.objects.filter(pk=fight.pk).update(data_version=fight.data_version + 1)
        with (
            offline(),
            mock.patch("fightlog.app_settings.FIGHTLOG_ZKB_ENABLED", True),
            mock.patch("fightlog.tasks.link_zkill.apply_async") as link,
        ):
            tasks.rebuild_fight.apply(args=[fight.pk])
        link.assert_called_once()
        with cache_lock(f"rebuild-{fight.pk}"):
            with mock.patch.object(tasks.rebuild_fight, "retry", side_effect=Retry()) as retry:
                with self.assertRaises(Retry):
                    tasks.rebuild_fight.run(fight.pk)
            retry.assert_called_once()

    def test_link_zkill_task(self):
        with offline():
            ingest_fixture(self.user, FIVE_LOGS)
        fight = max(Fight.objects.all(), key=lambda f: f.engagements.count())
        with mock.patch("fightlog.app_settings.FIGHTLOG_ZKB_ENABLED", False):
            tasks.link_zkill(fight.pk)
        self.assertIsNone(Fight.objects.get(pk=fight.pk).zkb_checked_at)
        recent_end = timezone.now() - timedelta(minutes=30)
        Fight.objects.filter(pk=fight.pk).update(end_at=recent_end)
        with (
            offline(zkb=True),
            mock.patch("fightlog.app_settings.FIGHTLOG_ZKB_ENABLED", True),
            mock.patch("fightlog.tasks.link_zkill.apply_async") as recheck,
            self.captureOnCommitCallbacks(execute=False),
        ):
            tasks.link_zkill(fight.pk, 0)
        self.assertIsNotNone(Fight.objects.get(pk=fight.pk).zkb_checked_at)
        recheck.assert_called_once()
        tasks.link_zkill(999999)

    def test_link_zkill_retries(self):
        fight = Fight.objects.create(title="x", start_at=timezone.now(), end_at=timezone.now())
        with mock.patch("fightlog.app_settings.FIGHTLOG_ZKB_ENABLED", True):
            for exc in (zkb.ZkbRateLimited(timezone.now().timestamp() + 5), requests.ConnectionError("down")):
                with (
                    mock.patch("fightlog.services.zkb.link_fight", side_effect=exc),
                    mock.patch.object(tasks.link_zkill, "retry", side_effect=Retry()),
                ):
                    with self.assertRaises(Retry):
                        tasks.link_zkill.run(fight.pk)

    def test_discord_task(self):
        with offline():
            ingest_fixture(self.user, [SAMPLE_LOG])
        webhook = DiscordWebhook.objects.create(name="ops", url="https://discord.invalid/1")
        post = DiscordPost.objects.create(fight=Fight.objects.get(), webhook=webhook, user=self.user)
        with (
            mock.patch("fightlog.services.discord.send", side_effect=lambda p: p),
            mock.patch("fightlog.tasks.notify") as notify,
        ):
            tasks.post_fight_to_discord(post.pk)
        notify.assert_called_once()
        with (
            mock.patch("fightlog.services.discord.send", side_effect=requests.ConnectionError("x")),
            mock.patch("fightlog.tasks.notify"),
        ):
            tasks.post_fight_to_discord(post.pk)
        post.refresh_from_db()
        self.assertEqual(post.status, DiscordPost.Status.FAILED)
        from fightlog.services.discord import DiscordRateLimited

        with (
            mock.patch("fightlog.services.discord.send", side_effect=DiscordRateLimited(2)),
            mock.patch.object(tasks.post_fight_to_discord, "retry", side_effect=Retry()),
        ):
            with self.assertRaises(Retry):
                tasks.post_fight_to_discord.run(post.pk)
        tasks.post_fight_to_discord(999999)

    def test_reparse_and_purge(self):
        log = ingest.accept_upload(self.user, fixture_filename(SAMPLE_LOG), fixture_bytes(SAMPLE_LOG)).log
        with mock.patch("fightlog.tasks.process_log.apply_async") as queue:
            self.assertEqual(tasks.reparse_all(), 1)
        queue.assert_called_once()
        CombatLog.objects.filter(pk=log.pk).update(
            status=CombatLog.Status.NO_PVP, purge_after=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(tasks.purge_expired_logs(), 1)

    def test_locks(self):
        with cache_lock("x", blocking=False):
            with self.assertRaises(LockBusy):
                with cache_lock("x", blocking=False):
                    pass


class CommandTest(TestCase):
    def test_reparse_and_rebuild_commands(self):
        user = fleet_owner()
        ingest.accept_upload(user, fixture_filename(SAMPLE_LOG), fixture_bytes(SAMPLE_LOG))
        out = StringIO()
        with offline():
            call_command("fightlog_reparse", "--sync", stdout=out)
            self.assertIn("rebuilt 1 fights", out.getvalue())
            call_command("fightlog_rebuild", "--sync", stdout=out)
        self.assertIn("Rebuilt 1 fights", out.getvalue())
        with (
            mock.patch("fightlog.tasks.process_log.apply_async") as queue,
            self.captureOnCommitCallbacks(execute=False),
        ):
            call_command("fightlog_reparse", "--all", stdout=out)
            call_command("fightlog_rebuild", stdout=out)
        queue.assert_called()
        self.assertIn("Queued 1 rebuilds", out.getvalue())
