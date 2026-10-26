"""Views: permissions, pages, JSON API, upload, curation POSTs, Discord and allies."""

import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse

from fightlog.auth_hooks import FightlogMenuItem, register_menu, register_urls
from fightlog.models import CombatLog, DiscordPost, DiscordWebhook, Fight, FightRedirect, FriendlyEntity, Participant
from fightlog.services import curation

from .utils import (
    ALL_PERMS,
    FIVE_LOGS,
    SAMPLE_LOG,
    FakeEsi,
    fixture_bytes,
    fixture_filename,
    fleet_owner,
    ingest_fixture,
    make_user,
    offline,
)


class ViewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = fleet_owner(permissions=["fightlog.basic_access"])
        with offline():
            ingest_fixture(cls.owner, [SAMPLE_LOG, FIVE_LOGS[1]])
        cls.fight = max(Fight.objects.all(), key=lambda f: f.engagements.count())
        cls.member = make_user("member")
        cls.curator = make_user("curator", permissions=ALL_PERMS)
        cls.outsider = make_user("outsider", permissions=[])

    def login(self, user):
        self.client.force_login(user)


class PermissionTest(ViewTestCase):
    def test_pages_need_basic_access(self):
        urls = [
            reverse("fightlog:index"),
            reverse("fightlog:fight", args=[self.fight.pk]),
            reverse("fightlog:upload"),
            reverse("fightlog:my_logs"),
            reverse("fightlog:help"),
            reverse("fightlog:api_analysis", args=[self.fight.pk]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.login(self.outsider)
        for url in urls:
            self.assertNotEqual(self.client.get(url).status_code, 200, url)
        self.login(self.member)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertNotEqual(self.client.get(reverse("fightlog:friendlies")).status_code, 200)

    def test_menu_item(self):
        request = RequestFactory().get("/")
        request.user = self.member
        self.assertIn("Fight Logs", FightlogMenuItem().render(request))
        request.user = self.outsider
        self.assertEqual(FightlogMenuItem().render(request), "")
        self.assertIsInstance(register_menu(), FightlogMenuItem)
        self.assertTrue(register_urls())


class PagesTest(ViewTestCase):
    def test_fight_list_and_filters(self):
        self.login(self.member)
        url = reverse("fightlog:index")
        page = self.client.get(url)
        self.assertContains(page, self.fight.title)
        for query, hit in (
            ({"who": "IMBCN"}, True),
            ({"who": "WEFEW"}, True),
            ({"who": "Pilot 001"}, True),
            ({"who": "nobody"}, False),
            ({"ship": "Sleipnir"}, True),
            ({"ship": "Avatar"}, False),
            ({"text": "WEFEW"}, True),
            ({"min_pilots": "500"}, False),
            ({"ours": "on"}, True),
            ({"date_from": "2026-10-26"}, False),
            ({"date_to": "2026-10-26", "date_from": "2026-10-25"}, True),
            ({"uploader": "owner"}, True),
            ({"system": "Jita"}, False),
            ({"date_from": "not a date"}, True),
        ):
            response = self.client.get(url, query)
            self.assertEqual(self.fight.title in response.content.decode(), hit, query)

    def test_fight_detail(self):
        self.login(self.member)
        response = self.client.get(reverse("fightlog:fight", args=[self.fight.pk]))
        self.assertContains(response, "fight-config")
        self.assertContains(response, "Pilot 001")
        self.assertNotContains(response, "Edit this fight")
        self.login(self.owner)
        self.assertContains(self.client.get(reverse("fightlog:fight", args=[self.fight.pk])), "Edit this fight")
        self.assertEqual(self.client.get(reverse("fightlog:fight", args=[999999])).status_code, 404)
        FightRedirect.objects.create(old_id=424242, fight=self.fight)
        moved = self.client.get(reverse("fightlog:fight", args=[424242]))
        self.assertEqual(moved.status_code, 301)

    def test_help_upload_and_my_logs(self):
        self.login(self.owner)
        self.assertContains(self.client.get(reverse("fightlog:help")), "overview ship label")
        self.assertContains(self.client.get(reverse("fightlog:upload")), "dropzone")
        self.assertContains(self.client.get(reverse("fightlog:my_logs")), "Pilot 001")
        self.login(self.member)
        self.assertContains(self.client.get(reverse("fightlog:my_logs")), "haven't uploaded")


class ApiTest(ViewTestCase):
    def test_analysis(self):
        self.login(self.member)
        url = reverse("fightlog:api_analysis", args=[self.fight.pk])
        data = self.client.get(url).json()
        self.assertTrue(data["built"])
        self.assertTrue(data["pilots"])
        self.assertTrue(data["series"]["damage_out"])
        log_ids = ",".join(str(i) for i in data["active_logs"][:1])
        one = self.client.get(
            url, {"logs": log_ids + ",x,99999", "bucket": "7", "non_ships": "1", "perspective": "pilot:p:Pilot 001"}
        ).json()
        self.assertEqual(len(one["active_logs"]), 1)
        self.assertEqual(one["bucket"], 10)
        self.assertEqual(one["perspective"], "pilot:p:Pilot 001")
        self.assertEqual(self.client.get(reverse("fightlog:api_analysis", args=[999999])).status_code, 404)

    def test_events_pilot_status(self):
        self.login(self.member)
        start = int(self.fight.start_at.timestamp())
        events = self.client.get(
            reverse("fightlog:api_events", args=[self.fight.pk]),
            {"start": start, "end": start + 120, "kinds": "damage,bogus", "limit": "5"},
        ).json()
        self.assertEqual(len(events["events"]), 5)
        self.assertGreater(events["total"], 5)
        pilot = self.client.get(reverse("fightlog:api_pilot", args=[self.fight.pk]), {"key": "p:Pilot 001"}).json()
        self.assertTrue(pilot["weapons"])
        self.assertTrue(pilot["participant"]["ship_rows"])
        self.assertEqual(self.client.get(reverse("fightlog:api_pilot", args=[self.fight.pk])).status_code, 404)
        status = self.client.get(reverse("fightlog:api_fight_status", args=[self.fight.pk])).json()
        self.assertTrue(status["built"])


class UploadApiTest(ViewTestCase):
    def test_upload_flow(self):
        self.login(self.owner)
        url = reverse("fightlog:api_upload")
        self.assertEqual(self.client.post(url).status_code, 400)
        name = FIVE_LOGS[2]
        with self.captureOnCommitCallbacks(execute=False), offline():
            response = self.client.post(url, {"file": SimpleUploadedFile(fixture_filename(name), fixture_bytes(name))})
        self.assertEqual(response.status_code, 200)
        log_id = response.json()["log_id"]
        status = self.client.get(reverse("fightlog:api_log", args=[log_id])).json()
        self.assertEqual(status["status"], "pending")
        self.login(self.member)
        rejected = self.client.post(url, {"file": SimpleUploadedFile(fixture_filename(name), fixture_bytes(name))})
        self.assertEqual(rejected.status_code, 422)
        with mock.patch("fightlog.app_settings.FIGHTLOG_MAX_UPLOAD_MB", 0):
            big = self.client.post(url, {"file": SimpleUploadedFile("x.txt", b"abc")})
        self.assertEqual(big.status_code, 413)
        self.assertEqual(self.client.get(reverse("fightlog:api_log", args=[999999])).status_code, 404)

    def test_delete_rights(self):
        log = CombatLog.objects.filter(listener_name="Pilot 006").first()
        self.login(self.member)
        self.assertEqual(self.client.delete(reverse("fightlog:api_log", args=[log.pk])).status_code, 403)
        response = self.client.post(reverse("fightlog:delete_log", args=[log.pk]), {"next": "https://evil.example/"})
        self.assertRedirects(response, reverse("fightlog:my_logs"), fetch_redirect_response=False)
        self.assertTrue(CombatLog.objects.filter(pk=log.pk).exists())
        self.login(self.owner)
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                reverse("fightlog:delete_log", args=[log.pk]), {"next": reverse("fightlog:index")}
            )
        self.assertRedirects(response, reverse("fightlog:index"), fetch_redirect_response=False)
        self.assertFalse(CombatLog.objects.filter(pk=log.pk).exists())
        other = CombatLog.objects.first()
        with self.captureOnCommitCallbacks(execute=False):
            self.assertEqual(
                self.client.delete(reverse("fightlog:api_log", args=[other.pk])).json()["status"], "deleted"
            )


class CurationViewTest(ViewTestCase):
    def edit(self, action, data, user=None):
        self.login(user or self.owner)
        with self.captureOnCommitCallbacks(execute=False), offline():
            return self.client.post(reverse("fightlog:edit", args=[self.fight.pk, action]), data)

    def test_member_cannot_edit(self):
        self.assertEqual(self.edit("rename", {"title": "x"}, self.member).status_code, 403)
        self.assertEqual(self.edit("bogus", {}).status_code, 403)

    def test_text_actions(self):
        self.assertEqual(self.edit("rename", {"title": "Renamed"}).status_code, 302)
        self.edit("notes", {"notes": "n"})
        self.edit("tags", {"tags": "#a, b"})
        self.edit("system", {"system": "Jita"})
        self.edit("system", {"system": "Nope"})
        fight = Fight.objects.get(pk=self.fight.pk)
        self.assertEqual((fight.title, fight.notes, fight.system.name), ("Renamed", "n", "Jita"))
        self.assertEqual(fight.tags.count(), 2)

    def test_sides_and_attribution(self):
        side = self.fight.sides.get(order=0)
        key = self.fight.participants.filter(corporation_ticker="YAGAS").first().key
        self.edit("side", {"keys": [key], "side": side.pk})
        self.edit("side", {"keys": [key], "side": "0"})
        self.edit("side_rename", {"side": side.pk, "name": "WE"})
        self.assertEqual(self.fight.sides.get(order=0).name, "WE")
        actor = self.fight.participants.filter(kind=Participant.Kind.SHIP_ONLY).first()
        self.edit("attribute", {"actor": actor.key, "pilot": "p:Pilot 006"})
        self.edit("attribute", {"actor": "p:x", "pilot": "p:y"})
        self.assertEqual(self.fight.attribution_overrides.count(), 1)

    def test_logs_split_merge_move(self):
        log = CombatLog.objects.filter(listener_name="Pilot 006").first()
        self.edit("exclude", {"log": log.pk, "reason": "bad"})
        log.refresh_from_db()
        self.assertTrue(log.excluded)
        self.edit("include", {"log": log.pk})
        self.edit("split", {"at": "not a time"})
        mid = self.fight.start_at + (self.fight.end_at - self.fight.start_at) / 2
        self.edit("split", {"at": mid.strftime("%Y-%m-%d %H:%M:%S")})
        self.assertEqual(Fight.objects.filter(start_at__gte=mid).count() >= 1, True)
        other = Fight.objects.exclude(pk=self.fight.pk).order_by("-pk").first()
        engagement = self.fight.engagements.first()
        self.edit("move", {"engagement": engagement.pk, "target": other.pk})
        self.assertEqual(Fight.objects.get(pk=other.pk).engagements.filter(pk=engagement.pk).count(), 1)
        stranger_fight = Fight.objects.create(title="s", start_at=self.fight.start_at, end_at=self.fight.end_at)
        self.assertEqual(self.edit("merge", {"other": stranger_fight.pk}, self.owner).status_code, 403)
        response = self.edit("merge", {"other": other.pk})
        self.assertEqual(response.status_code, 302)


class DiscordAndAlliesTest(ViewTestCase):
    def test_post_to_discord(self):
        webhook = DiscordWebhook.objects.create(name="ops", url="https://discord.invalid/1")
        self.login(self.member)
        url = reverse("fightlog:discord", args=[self.fight.pk])
        self.client.post(url, {"webhook": webhook.pk})
        self.assertEqual(DiscordPost.objects.count(), 0)
        self.login(self.curator)
        detail = self.client.get(reverse("fightlog:fight", args=[self.fight.pk]))
        self.assertContains(detail, "Post to Discord")
        with mock.patch("fightlog.tasks.post_fight_to_discord.apply_async") as send:
            response = self.client.post(url, {"webhook": webhook.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DiscordPost.objects.count(), 1)
        send.assert_called_once()
        self.assertFalse(Fight.objects.get(pk=self.fight.pk).is_curated)

    def test_allies_page(self):
        self.login(self.curator)
        url = reverse("fightlog:friendlies")
        self.assertEqual(self.client.get(url).status_code, 200)
        with FakeEsi().patched():
            self.client.post(url, {"entity_type": "corporation", "identifier": "Baba Yagas"})
            self.client.post(url, {"entity_type": "alliance", "identifier": "99011699"})
            self.client.post(url, {"entity_type": "alliance", "identifier": "No Such Alliance"})
        self.assertEqual(sorted(FriendlyEntity.objects.values_list("ticker", flat=True)), ["WEFEW", "YAGAS"])
        entity = FriendlyEntity.objects.first()
        self.client.post(url, {"delete": entity.pk})
        self.assertEqual(FriendlyEntity.objects.count(), 1)


__all__ = ["curation", "json"]
