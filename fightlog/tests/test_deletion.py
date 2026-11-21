"""Deleting fights: mark as deleted, restore, purge, and what everyone sees in between."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fightlog import app_settings, tasks
from fightlog.locks import LockBusy, cache_lock
from fightlog.models import CombatLog, DiscordWebhook, Engagement, Fight, FightChange
from fightlog.services import curation, deletion, ingest
from fightlog.services.fights import can_edit, can_view
from fightlog.services.rebuild import FightRebuilder

from .test_parser import DMG_OUT, HEADER
from .test_views import ViewTestCase
from .utils import ALL_PERMS, FIVE_LOGS, fixture_bytes, fixture_filename, fleet_owner, make_user, offline

EARLY, LATE = "20:19:", "22:40:"
ENEMY_A, ENEMY_B = "A[X](Rifter)", "B[Y](Rifter)"


def combat_log(listener: str, *stretches: tuple[str, str], hits: int = 8) -> bytes:
    """A gamelog with ``hits`` hits on ``label``, one a second, from each ``minute``."""
    lines = [HEADER.replace("Pilot Listener", listener)]
    for minute, label in stretches:
        for i in range(hits):
            hit = DMG_OUT.format(n=100 + i, label=label, weapon="425mm AutoCannon II", q="Hits")
            lines.append(f"[ 2026.10.25 {minute}{i:02d} ] (combat) {hit}\n")
    return "".join(lines).encode("utf-8")


class DeletionTestCase(TestCase):
    """Two fights an evening apart. Pilot 001's log is in both, Pilot 006's only in the early one."""

    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner(permissions=ALL_PERMS)
        cls.shared = cls.upload(combat_log("Pilot 001", (EARLY, ENEMY_A), (LATE, ENEMY_B)), 90000001)
        cls.single = cls.upload(combat_log("Pilot 006", (EARLY, ENEMY_A)), 90000006)

    @classmethod
    def upload(cls, data: bytes, character_id: int) -> CombatLog:
        log = ingest.accept_upload(cls.user, f"20261025_195145_{character_id}.txt", data).log
        ingest.process_log(log)
        return log

    def setUp(self):
        self.early, self.late = Fight.objects.order_by("start_at")

    def delete(self, fight: Fight | None = None, reason: str = "") -> Fight:
        fight = fight or self.early
        self.assertTrue(deletion.delete_fight(fight, self.user, reason))
        return fight


class DeleteAndRestoreTest(DeletionTestCase):
    def test_fixture(self):
        self.assertEqual(Fight.objects.count(), 2)
        self.assertEqual(self.early.engagements.count(), 2)
        self.assertEqual(self.late.engagements.count(), 1)

    def test_delete_marks_the_fight_and_keeps_everything(self):
        before = timezone.now()
        fight = self.delete(reason="  a duel, not a fight  ")
        fight.refresh_from_db()
        self.assertTrue(fight.is_deleted)
        self.assertEqual((fight.deleted_by, fight.deleted_reason), (self.user, "a duel, not a fight"))
        days = (fight.purge_after - fight.deleted_at).days
        self.assertEqual(days, app_settings.FIGHTLOG_DELETED_FIGHT_RETENTION_DAYS)
        self.assertEqual(days, 7)
        self.assertGreaterEqual(fight.deleted_at, before)
        self.assertEqual(fight.engagements.count(), 2)
        self.assertEqual(CombatLog.objects.count(), 2)
        change = fight.changes.get()
        self.assertEqual((change.action, change.user), (FightChange.Action.DELETE, self.user))
        self.assertFalse(fight.is_curated)
        # Deleting twice changes nothing.
        self.assertFalse(deletion.delete_fight(fight, self.user, "again"))
        self.assertEqual(Fight.objects.get(pk=fight.pk).deleted_reason, "a duel, not a fight")

    def test_the_retention_is_a_setting(self):
        with mock.patch.object(app_settings, "FIGHTLOG_DELETED_FIGHT_RETENTION_DAYS", 30):
            fight = self.delete()
            self.assertEqual(deletion.retention_label(), "30 days")
        self.assertEqual((fight.purge_after - fight.deleted_at).days, 30)
        with mock.patch.object(app_settings, "FIGHTLOG_DELETED_FIGHT_RETENTION_DAYS", 1):
            self.assertEqual(deletion.retention_label(), "1 day")

    def test_restore_brings_it_back_as_it_was(self):
        fight = self.delete(reason="oops")
        with (
            mock.patch.object(app_settings, "FIGHTLOG_ZKB_ENABLED", True),
            mock.patch("fightlog.tasks.link_zkill.apply_async") as link,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertTrue(deletion.restore_fight(fight, self.user))
        link.assert_called_once()  # zKillboard checks were skipped while it was deleted
        fight.refresh_from_db()
        self.assertFalse(fight.is_deleted)
        self.assertEqual((fight.deleted_by, fight.deleted_reason, fight.purge_after), (None, "", None))
        self.assertEqual(fight.engagements.count(), 2)
        self.assertEqual(list(fight.changes.values_list("action", flat=True)), ["restore", "delete"])
        self.assertFalse(deletion.restore_fight(fight, self.user))

    def test_a_rebuild_in_progress_does_not_undo_a_delete(self):
        rebuilder = FightRebuilder(Fight.objects.get(pk=self.early.pk))  # its copy predates the delete
        self.delete()
        with offline():
            rebuilder.run()
        self.assertTrue(Fight.objects.get(pk=self.early.pk).is_deleted)

    def test_deleted_fights_are_for_curators_only_and_read_only(self):
        member = make_user("member")
        self.assertTrue(can_view(member, self.early))
        self.assertTrue(can_edit(self.user, self.early))
        fight = self.delete()
        self.assertFalse(can_view(member, fight))
        self.assertTrue(can_view(self.user, fight))
        self.assertFalse(can_edit(self.user, fight))

    def test_zkill_is_not_asked_about_deleted_fights(self):
        fight = self.delete()
        with (
            mock.patch.object(app_settings, "FIGHTLOG_ZKB_ENABLED", True),
            mock.patch("fightlog.services.zkb.link_fight") as link,
        ):
            tasks.link_zkill(fight.pk)
        link.assert_not_called()


class LateUploadTest(DeletionTestCase):
    def test_a_late_log_joins_the_deleted_fight(self):
        fight = self.delete()
        late_log = self.upload(combat_log("Pilot 007", (EARLY, ENEMY_A)), 90000007)
        self.assertEqual(Fight.objects.count(), 2)
        self.assertEqual(Engagement.objects.get(log=late_log).fight, fight)
        self.assertTrue(Fight.objects.get(pk=fight.pk).is_deleted)

    def test_an_observer_log_joins_the_deleted_fight_it_overlaps(self):
        # No named pilots in it, so it can only be placed by time (third-party tackle seen from off grid).
        fight = self.delete()
        name = FIVE_LOGS[4]
        log = ingest.accept_upload(self.user, fixture_filename(name), fixture_bytes(name)).log
        ingest.process_log(log)
        self.assertEqual(Fight.objects.count(), 2)
        self.assertEqual(set(log.engagements.values_list("fight", flat=True)), {fight.pk})

    def test_a_visible_fight_wins_over_a_deleted_one(self):
        # A second fight at the same time, against other people: the two only share a later log.
        self.upload(combat_log("Pilot 013", (EARLY, "C[Z](Rifter)")), 90000013)
        self.assertEqual(Fight.objects.count(), 3)
        visible = Fight.objects.get(engagements__log__listener_name="Pilot 013")
        self.delete()
        bridge = self.upload(combat_log("Pilot 007", (EARLY, ENEMY_A), (EARLY, "C[Z](Rifter)")), 90000007)
        self.assertEqual(Engagement.objects.get(log=bridge).fight, visible)
        self.assertEqual(Fight.objects.count(), 3)  # and nothing was merged

    def test_deleted_fights_are_never_merged(self):
        self.upload(combat_log("Pilot 013", (EARLY, "C[Z](Rifter)")), 90000013)
        other = Fight.objects.get(engagements__log__listener_name="Pilot 013")
        self.delete()
        self.delete(other)
        bridge = self.upload(combat_log("Pilot 007", (EARLY, ENEMY_A), (EARLY, "C[Z](Rifter)")), 90000007)
        self.assertEqual(Fight.objects.count(), 3)
        self.assertTrue(Engagement.objects.get(log=bridge).fight.is_deleted)


class PurgeTest(DeletionTestCase):
    def test_merge_ranges(self):
        self.assertEqual(
            deletion.merge_ranges([[50, 60], [1, 5], [5, 9], [20, 30], [25, 26]]), [[1, 9], [20, 30], [50, 60]]
        )
        self.assertEqual(deletion.merge_ranges([]), [])

    def test_purge_deletes_the_fight_and_the_logs_only_it_uses(self):
        fight = self.delete()
        span = [
            [int(e.start_at.timestamp()), int(e.end_at.timestamp())] for e in fight.engagements.filter(log=self.shared)
        ]
        result = deletion.purge_fight(fight, self.user)
        self.assertEqual((result.fights, result.logs_deleted, result.logs_trimmed), (1, 1, 1))
        self.assertFalse(Fight.objects.filter(pk=fight.pk).exists())
        self.assertFalse(CombatLog.objects.filter(pk=self.single.pk).exists())
        # The log the later fight also uses stays, without the deleted fight's part.
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.purged_ranges, span)
        self.assertEqual(list(self.shared.engagements.values_list("fight", flat=True)), [self.late.pk])
        self.late.refresh_from_db()
        self.assertFalse(self.late.is_deleted)

    def test_a_reparse_does_not_bring_a_purged_fight_back(self):
        deletion.purge_fight(self.delete())
        self.shared.refresh_from_db()
        ingest.process_log(self.shared)
        engagement = self.shared.engagements.get()
        self.assertEqual(f"{engagement.start_at:%H:%M}", LATE[:5])
        self.assertFalse(Fight.objects.filter(start_at__hour=20).exists())

    def test_purging_half_of_a_split_fight_trims_the_log_to_the_second(self):
        # One 20-second engagement, cut in two by a curator: the purged half's range ends where the kept half begins.
        log = self.upload(combat_log("Pilot 007", ("21:30:", "D[W](Rifter)"), hits=20), 90000007)
        first_half = Fight.objects.get(engagements__log=log)
        start, cut = first_half.start_at, first_half.start_at + timedelta(seconds=10)
        second_half = curation.split_fight(first_half, cut, self.user)
        deletion.purge_fight(self.delete(second_half))
        log.refresh_from_db()
        self.assertEqual(log.purged_ranges, [[int(cut.timestamp()), int(cut.timestamp()) + 9]])
        kept = log.engagements.get()
        self.assertEqual((kept.fight, kept.event_count), (first_half, 10))
        # A re-parse finds the whole engagement again and leaves out exactly the purged seconds, ends included.
        ingest.process_log(log)
        again = log.engagements.get()
        self.assertEqual((again.event_count, again.start_at, again.end_at), (10, start, cut - timedelta(seconds=1)))

    def test_each_purge_adds_its_range_to_a_shared_log(self):
        stretches = [("20:40:", "D[W](Rifter)"), ("21:10:", "E[V](Rifter)"), ("21:50:", "F[U](Rifter)")]
        log = self.upload(combat_log("Pilot 007", *stretches), 90000007)
        first, second, third = Fight.objects.filter(engagements__log=log).order_by("start_at")
        deletion.purge_fight(self.delete(first))
        deletion.purge_fight(self.delete(second))
        log.refresh_from_db()
        self.assertEqual(len(log.purged_ranges), 2)
        self.assertEqual(list(log.engagements.values_list("fight", flat=True)), [third.pk])
        ingest.process_log(log)
        self.assertEqual([f"{e.start_at:%H:%M}" for e in log.engagements.all()], ["21:50"])

    def test_a_longer_copy_of_a_trimmed_log_stays_trimmed(self):
        deletion.purge_fight(self.delete())
        longer = combat_log("Pilot 001", (EARLY, ENEMY_A), (LATE, ENEMY_B), ("23:50:", ENEMY_B))
        result = ingest.accept_upload(self.user, "20261025_195145_90000001.txt", longer)
        self.assertEqual((result.status, result.log), ("replaced", self.shared))
        ingest.process_log(result.log)
        self.assertEqual([f"{e.start_at:%H:%M}" for e in self.shared.engagements.all()], ["22:40", "23:50"])

    def test_a_log_whose_fights_are_all_purged_goes_with_the_last_one(self):
        deletion.purge_fight(self.delete())
        self.assertTrue(CombatLog.objects.filter(pk=self.shared.pk).exists())
        result = deletion.purge_fight(self.delete(self.late))
        self.assertEqual((result.logs_deleted, result.logs_trimmed), (1, 0))
        self.assertEqual((Fight.objects.count(), CombatLog.objects.count(), Engagement.objects.count()), (0, 0, 0))

    def test_only_deleted_fights_can_be_purged(self):
        self.assertIsNone(deletion.purge_fight(self.early))
        self.assertEqual(Fight.objects.count(), 2)
        self.assertEqual(CombatLog.objects.count(), 2)

    def test_a_fight_being_rebuilt_is_not_purged(self):
        fight = self.delete()
        with cache_lock(f"rebuild-{fight.pk}"):
            with self.assertRaises(LockBusy):
                deletion.purge_fight(fight)
            Fight.objects.filter(pk=fight.pk).update(purge_after=timezone.now() - timedelta(minutes=1))
            self.assertEqual(deletion.purge_expired_fights().fights, 0)
        self.assertTrue(Fight.objects.filter(pk=fight.pk).exists())
        self.assertEqual(deletion.purge_expired_fights().fights, 1)

    def test_the_daily_task_purges_once_the_retention_is_over(self):
        fight = self.delete()
        self.assertEqual(tasks.purge_expired_logs(), 0)
        self.assertTrue(Fight.objects.filter(pk=fight.pk).exists())
        Fight.objects.filter(pk=fight.pk).update(purge_after=timezone.now() - timedelta(minutes=1))
        self.assertEqual(tasks.purge_expired_logs(), 1)
        self.assertFalse(Fight.objects.filter(pk=fight.pk).exists())
        self.assertTrue(Fight.objects.filter(pk=self.late.pk).exists())

    def test_a_restored_fight_is_not_purged(self):
        fight = self.delete()
        Fight.objects.filter(pk=fight.pk).update(purge_after=timezone.now() - timedelta(minutes=1))
        deletion.restore_fight(fight, self.user)
        self.assertEqual(deletion.purge_expired_fights().fights, 0)
        self.assertEqual(Fight.objects.count(), 2)


class DeletionViewTest(ViewTestCase):
    def post(self, name: str, user, data=None, fight=None):
        self.login(user)
        with self.captureOnCommitCallbacks(execute=False):
            return self.client.post(reverse(f"fightlog:{name}", args=[(fight or self.fight).pk]), data or {})

    def deleted(self, reason: str = "") -> Fight:
        deletion.delete_fight(self.fight, self.curator, reason)
        return Fight.objects.get(pk=self.fight.pk)

    def test_the_permission_says_what_it_allows(self):
        self.assertIn("delete any fight", Permission.objects.get(codename="curate_fights").name)

    def test_only_curators_delete_and_restore(self):
        for user in (self.member, self.owner, self.outsider):  # the owner uploaded this fight's logs
            self.assertEqual(self.post("delete_fight", user).status_code, 302)
            self.assertFalse(Fight.objects.get(pk=self.fight.pk).is_deleted)
        response = self.post("delete_fight", self.curator, {"reason": "test fleet"})
        self.assertRedirects(response, self.fight.get_absolute_url(), fetch_redirect_response=False)
        fight = Fight.objects.get(pk=self.fight.pk)
        self.assertEqual((fight.is_deleted, fight.deleted_reason, fight.deleted_by), (True, "test fleet", self.curator))
        for user in (self.member, self.owner):
            self.post("restore_fight", user)
            self.post("purge_fight", user)
            self.assertTrue(Fight.objects.get(pk=self.fight.pk).is_deleted)
        self.post("restore_fight", self.curator)
        self.assertFalse(Fight.objects.get(pk=self.fight.pk).is_deleted)
        self.assertEqual(self.client.get(reverse("fightlog:delete_fight", args=[self.fight.pk])).status_code, 405)

    def test_the_fight_list_hides_deleted_fights(self):
        url = reverse("fightlog:index")
        self.login(self.curator)
        page = self.client.get(url)
        self.assertContains(page, self.fight.title)
        self.assertContains(page, "Show deleted")
        self.assertContains(page, "bulk-form")
        self.deleted("not a real fight")
        self.assertNotContains(self.client.get(url), self.fight.title)
        shown = self.client.get(url, {"deleted": "on"})
        self.assertContains(shown, self.fight.title)
        self.assertContains(shown, "Deleted by curator")
        self.assertContains(shown, "not a real fight")
        self.assertContains(shown, "Restore selected")
        # Searching still works on deleted fights, and the option survives paging.
        self.assertContains(self.client.get(url, {"deleted": "on", "who": "Pilot 001"}), self.fight.title)
        self.assertEqual(shown.context["query"], "deleted=on")
        # The option does nothing for anyone else, uploaders included.
        for user in (self.member, self.owner):
            self.login(user)
            page = self.client.get(url, {"deleted": "on"})
            self.assertNotContains(page, self.fight.title)
            self.assertNotContains(page, "Show deleted")
            self.assertNotContains(page, "bulk-form")

    def test_a_deleted_fight_is_not_found_except_by_curators(self):
        self.deleted("why")
        pk = self.fight.pk
        urls = [
            reverse("fightlog:fight", args=[pk]),
            reverse("fightlog:api_analysis", args=[pk]),
            reverse("fightlog:api_events", args=[pk]),
            reverse("fightlog:api_pilot", args=[pk]) + "?key=p:Pilot 001",
            reverse("fightlog:api_fight_status", args=[pk]),
        ]
        for user in (self.member, self.owner):
            self.login(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 404, url)
            rename = self.client.post(reverse("fightlog:edit", args=[pk, "rename"]), {"title": "x"})
            self.assertEqual(rename.status_code, 404)
        self.login(self.curator)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        page = self.client.get(urls[0])
        self.assertContains(page, "This fight is deleted.")
        self.assertContains(page, "Reason: why")
        self.assertContains(page, "Restore")
        self.assertContains(page, "Delete for good now")
        self.assertNotContains(page, "Edit this fight")
        self.assertNotContains(page, "Post to Discord")

    def test_a_deleted_fight_cannot_be_edited_merged_into_or_posted(self):
        other = Fight.objects.create(title="Other", start_at=self.fight.start_at, end_at=self.fight.end_at)
        self.deleted()
        self.login(self.curator)
        url = reverse("fightlog:edit", args=[self.fight.pk, "rename"])
        self.assertEqual(self.client.post(url, {"title": "x"}).status_code, 403)
        # A deleted fight isn't offered as a merge or move target, and is refused as one.
        self.assertNotIn(self.fight, self.client.get(other.get_absolute_url()).context["other_fights"])
        merge = self.client.post(reverse("fightlog:edit", args=[other.pk, "merge"]), {"other": self.fight.pk})
        self.assertEqual(merge.status_code, 403)
        self.assertTrue(Fight.objects.filter(pk=self.fight.pk).exists())
        webhook = DiscordWebhook.objects.create(name="ops", url="https://discord.invalid/1")
        post = self.client.post(reverse("fightlog:discord", args=[self.fight.pk]), {"webhook": webhook.pk})
        self.assertEqual(post.status_code, 404)

    def test_the_delete_card_is_for_curators(self):
        url = reverse("fightlog:fight", args=[self.fight.pk])
        self.login(self.owner)
        self.assertNotContains(self.client.get(url), "delete-form")
        self.login(self.curator)
        page = self.client.get(url)
        self.assertContains(page, "delete-form")
        self.assertContains(page, "restore it for 7 days")

    def test_purge_now(self):
        self.assertEqual(self.post("purge_fight", self.curator).status_code, 302)
        self.assertTrue(Fight.objects.filter(pk=self.fight.pk).exists())  # delete it first
        self.deleted()
        log_ids = set(self.fight.engagements.values_list("log", flat=True))
        with cache_lock(f"rebuild-{self.fight.pk}"):
            self.post("purge_fight", self.curator)
        self.assertTrue(Fight.objects.filter(pk=self.fight.pk).exists())
        response = self.post("purge_fight", self.curator)
        self.assertRedirects(response, reverse("fightlog:index") + "?deleted=on", fetch_redirect_response=False)
        self.assertFalse(Fight.objects.filter(pk=self.fight.pk).exists())
        shared = set(Engagement.objects.filter(log__in=log_ids).values_list("log", flat=True))
        self.assertEqual(set(CombatLog.objects.filter(pk__in=log_ids).values_list("pk", flat=True)), shared)
        self.assertEqual(self.client.get(reverse("fightlog:fight", args=[self.fight.pk])).status_code, 404)

    def test_bulk_delete_and_restore(self):
        url = reverse("fightlog:bulk")
        ids = list(Fight.objects.values_list("pk", flat=True))
        back = reverse("fightlog:index") + "?deleted=on"
        self.login(self.owner)
        self.client.post(url, {"action": "delete", "fights": ids})
        self.assertFalse(Fight.objects.filter(deleted_at__isnull=False).exists())
        self.login(self.curator)
        self.assertEqual(self.client.post(url, {"action": "purge", "fights": ids}).status_code, 403)
        response = self.client.post(url, {"action": "delete", "fights": ids + ["x"], "reason": "cleanup", "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        self.assertEqual(Fight.objects.filter(deleted_reason="cleanup").count(), len(ids))
        evil = self.client.post(url, {"action": "restore", "fights": ids[:1], "next": "https://evil.example/"})
        self.assertRedirects(evil, reverse("fightlog:index"), fetch_redirect_response=False)
        self.assertEqual(Fight.objects.filter(deleted_at__isnull=False).count(), len(ids) - 1)
        self.client.post(url, {"action": "restore", "fights": ids})
        self.assertFalse(Fight.objects.filter(deleted_at__isnull=False).exists())
        nothing = self.client.post(url, {"action": "restore"}, follow=True)
        self.assertContains(nothing, "No fight was selected.", count=1)  # Alliance Auth's base shows it, once

    def test_uploaders_are_told_their_log_is_in_a_deleted_fight(self):
        fight = self.deleted()
        log = CombatLog.objects.filter(engagements__fight=fight).first()
        self.login(self.owner)
        page = self.client.get(reverse("fightlog:my_logs"))
        self.assertContains(page, "in a deleted fight")
        self.assertNotContains(page, fight.get_absolute_url())
        row = next(
            f for f in self.client.get(reverse("fightlog:api_log", args=[log.pk])).json()["fights"] if f["deleted"]
        )
        self.assertEqual(sorted(row), ["built", "deleted", "id"])  # no title, no link
        self.login(self.curator)
        row = next(
            f for f in self.client.get(reverse("fightlog:api_log", args=[log.pk])).json()["fights"] if f["deleted"]
        )
        self.assertEqual(row["url"], fight.get_absolute_url())
        # Uploaders can still delete their own log from a deleted fight.
        self.login(self.owner)
        with self.captureOnCommitCallbacks(execute=False):
            self.client.post(reverse("fightlog:delete_log", args=[log.pk]))
        self.assertFalse(CombatLog.objects.filter(pk=log.pk).exists())

    def test_help_explains_deleted_fights(self):
        self.login(self.member)
        self.assertContains(self.client.get(reverse("fightlog:help")), "For 7 days a curator can still restore it")


__all__ = ["curation"]
