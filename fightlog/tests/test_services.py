"""Services: ingest, clustering, rebuild helpers, curation, zKillboard, Discord, ESI resolution."""

import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

import requests
import requests_mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from fightlog import app_settings
from fightlog.models import (
    Alliance,
    CombatLog,
    CombatLogUpload,
    Corporation,
    DiscordPost,
    DiscordWebhook,
    Engagement,
    Fight,
    FightChange,
    FightRedirect,
    Killmail,
    Participant,
    ResolvedName,
    SideOverride,
    SolarSystem,
)
from fightlog.services import curation, discord, ingest, resolve, zkb
from fightlog.services.fights import can_edit, is_kept_when_empty
from fightlog.services.rebuild import fleet_periods, rebuild_fight, system_from_logs

from .test_parser import DMG_IN, DMG_OUT, log_text
from .utils import (
    FIVE_LOGS,
    SAMPLE_LOG,
    FakeEsi,
    fixture_bytes,
    fixture_filename,
    fleet_owner,
    ingest_fixture,
    make_user,
    offline,
    zkill_data,
)

ENEMY = "Pilot Kilo I[IMBCN](Cenotaph)"


def pvp_log(listener="Pilot 001", minute="20:19:", n=8, label=ENEMY):
    lines = [DMG_OUT.format(n=100 + i, label=label, weapon="425mm AutoCannon II", q="Hits") for i in range(n)]
    data = log_text(*lines, start=f"2026.10.25 {minute}")
    return data.replace(b"Pilot Listener", listener.encode())


class IngestTest(TestCase):
    def setUp(self):
        self.user = fleet_owner()

    def test_rejections(self):
        stranger = make_user("stranger")
        result = ingest.accept_upload(stranger, "20261025_195145_90000001.txt", fixture_bytes(SAMPLE_LOG))
        self.assertEqual(result.status, "rejected")
        self.assertIn("not one of your characters", result.message)
        self.assertFalse(ingest.accept_upload(self.user, "x.txt", b"no header here").ok)
        with mock.patch.object(app_settings, "FIGHTLOG_MAX_UPLOAD_MB", 0):
            self.assertIn("larger", ingest.accept_upload(self.user, "a.txt", b"x").message)

    def test_fc_can_upload_other_peoples_logs(self):
        fc = make_user("fc", permissions=["fightlog.basic_access", "fightlog.upload_any_log"])
        result = ingest.accept_upload(fc, "20261025_195145_90000001.txt", fixture_bytes(SAMPLE_LOG))
        self.assertEqual(result.status, "accepted")

    def test_duplicates_credit_the_second_uploader(self):
        data = fixture_bytes(SAMPLE_LOG)
        first = ingest.accept_upload(self.user, "20261025_195145_90000001.txt", data)
        again = ingest.accept_upload(self.user, "20261025_195145_90000001.txt", data)
        self.assertIn("already uploaded", again.message)
        fc = make_user("fc2", permissions=["fightlog.basic_access", "fightlog.upload_any_log"])
        dup = ingest.accept_upload(fc, "copy.txt", data)
        self.assertEqual((dup.status, dup.log), ("duplicate", first.log))
        self.assertEqual(CombatLogUpload.objects.filter(log=first.log).count(), 2)
        self.assertEqual(first.log.uploader, self.user)

    def test_supersede_by_byte_prefix(self):
        data = fixture_bytes(SAMPLE_LOG)
        name = "20261025_195145_90000001.txt"
        partial = data[: len(data) // 2]  # copied mid-write: the last line is cut
        first = ingest.accept_upload(self.user, name, partial)
        self.assertEqual(first.status, "accepted")
        longer = ingest.accept_upload(self.user, name, data)
        self.assertEqual((longer.status, longer.log), ("replaced", first.log))
        shorter = ingest.accept_upload(self.user, name, partial)
        self.assertEqual(shorter.status, "unchanged")
        other = ingest.accept_upload(self.user, name, data.replace(b"Grazes", b"Hits", 1) + b"x")
        self.assertEqual(other.status, "rejected")

    def test_renamed_file_finds_the_listener_by_name(self):
        result = ingest.accept_upload(self.user, "my fight.txt", fixture_bytes(SAMPLE_LOG))
        self.assertEqual(result.log.listener_character_id, 90000001)
        fc = make_user("fc3", permissions=["fightlog.basic_access", "fightlog.upload_any_log"])
        with FakeEsi().patched():
            other = ingest.accept_upload(fc, "renamed.txt", fixture_bytes(FIVE_LOGS[1]))
        self.assertEqual(other.log.listener_character_id, 90000006)

    def test_processing_outcomes(self):
        def session(data, n):
            return data.replace(b"Pilot Listener", b"Pilot 001").replace(b"19:51:45", f"19:51:{n:02d}".encode())

        npc_only = session(
            log_text(
                *[
                    DMG_IN.format(n=5, label="Centii Plague", weapon="", q="Hits").replace(" -  - ", " - ")
                    for _ in range(10)
                ]
            ),
            1,
        )
        log = ingest.accept_upload(self.user, "20261025_195101_90000001.txt", npc_only).log
        ingest.process_log(log)
        log.refresh_from_db()
        self.assertEqual(log.status, CombatLog.Status.NO_PVP)
        self.assertIsNotNone(log.purge_after)
        german = session(log_text(*["{} Schaden an X".format(i) for i in range(30)]), 2)
        log = ingest.accept_upload(self.user, "20261025_195102_90000001.txt", german).log
        ingest.process_log(log)
        log.refresh_from_db()
        self.assertEqual(log.status, CombatLog.Status.REJECTED)
        log = ingest.accept_upload(self.user, "20261025_195103_90000001.txt", session(pvp_log(), 3)).log
        with mock.patch.object(app_settings, "FIGHTLOG_MAX_BLOB_BYTES", 10):
            ingest.process_log(log)
        log.refresh_from_db()
        self.assertEqual(log.status, CombatLog.Status.FAILED)
        self.assertEqual(ingest.fight_ids_for_log(log), [])


class ClusteringTest(TestCase):
    def setUp(self):
        self.user = fleet_owner()

    def upload(self, data, name):
        log = ingest.accept_upload(self.user, name, data).log
        ingest.process_log(log)
        return log

    def test_overlapping_logs_sharing_pilots_join(self):
        self.upload(pvp_log("Pilot 001"), "20261025_201900_90000001.txt")
        self.upload(pvp_log("Pilot 006"), "20261025_201900_90000006.txt")
        self.assertEqual(Fight.objects.count(), 1)
        self.upload(pvp_log("Pilot 007", minute="23:10:"), "20261025_231000_90000007.txt")
        self.assertEqual(Fight.objects.count(), 2)

    def test_an_engagement_bridging_two_fights_merges_them_unless_curated(self):
        a = self.upload(pvp_log("Pilot 001", label="A[X](Rifter)"), "20261025_201900_90000001.txt")
        b = self.upload(pvp_log("Pilot 006", label="B[Y](Rifter)"), "20261025_201900_90000006.txt")
        self.assertEqual(Fight.objects.count(), 2)
        fight_a = Fight.objects.get(engagements__log=a)
        curation.rename(fight_a, "Curated", self.user)
        bridge = log_text(
            *[
                DMG_OUT.format(n=1 + i, label=lbl, weapon="425mm AutoCannon II", q="Hits")
                for i, lbl in enumerate(["A[X](Rifter)", "B[Y](Rifter)"] * 4)
            ]
        ).replace(b"Pilot Listener", b"Pilot 007")
        self.upload(bridge, "20261025_201900_90000007.txt")
        self.assertEqual(Fight.objects.count(), 2)  # the curated fight is never absorbed
        self.assertEqual(Fight.objects.get(pk=fight_a.pk).engagements.count(), 2)
        Fight.objects.update(is_curated=False, title_is_manual=False)
        FightChange.objects.all().delete()
        self.upload(bridge.replace(b"Pilot 007", b"Pilot 013"), "20261025_201900_90000013.txt")
        self.assertEqual(Fight.objects.count(), 1)
        self.assertEqual(FightRedirect.objects.count(), 1)
        self.assertTrue(b)

    def test_observer_logs_join_by_time(self):
        self.upload(pvp_log("Pilot 001"), "20261025_201900_90000001.txt")
        observer = fixture_bytes(FIVE_LOGS[4])
        log = ingest.accept_upload(self.user, fixture_filename(FIVE_LOGS[4]), observer).log
        log.refresh_from_db()
        ingest.process_log(log)
        self.assertEqual(Fight.objects.count(), 1)


class RebuildHelpersTest(TestCase):
    def test_fleet_periods(self):
        context = [
            {"t": 10, "kind": "fleet_invite", "name": "FC"},
            {"t": 20, "kind": "fleet_warp", "name": "FC"},
            {"t": 30, "kind": "regroup", "name": "Boss"},
            {"t": 40, "kind": "fleet_join", "name": "Other FC"},
            {"t": 50, "kind": "fleet_member", "name": "Alt"},
        ]
        periods = fleet_periods(context, 0)
        self.assertEqual(periods[0], (10, 40, {"FC", "Boss"}))
        self.assertEqual(periods[1][2], {"Other FC", "Alt"})
        self.assertEqual(fleet_periods([], None), [])

    def test_system_from_logs(self):
        class L:
            def __init__(self, context):
                self.context = context

        k_space = L([{"t": 100, "kind": "system", "system": "Jita"}])
        self.assertEqual(system_from_logs([k_space], 200, 300), ("Jita", False))
        stale = L([{"t": 100, "kind": "system", "system": "Nema"}, {"t": 150, "kind": "wspace_hint"}])
        self.assertEqual(system_from_logs([stale], 200, 300), (None, True))
        after = L([{"t": 400, "kind": "system", "system": "Nema"}])
        self.assertEqual(system_from_logs([after], 200, 300), (None, False))


class CurationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner(permissions=["fightlog.basic_access", "fightlog.curate_fights"])
        with offline():
            ingest_fixture(cls.user, [SAMPLE_LOG])

    def setUp(self):
        self.fight = Fight.objects.get()

    def rebuild(self):
        with offline():
            rebuild_fight(self.fight.pk)
        self.fight.refresh_from_db()

    def test_text_edits_mark_the_fight_curated(self):
        curation.rename(self.fight, "Hole fight", self.user)
        curation.set_notes(self.fight, "We lost the logi", self.user)
        curation.set_tags(self.fight, ["#lost", "home", " ", "#lost"], self.user)
        self.fight.refresh_from_db()
        self.assertEqual(self.fight.title, "Hole fight")
        self.assertTrue(self.fight.is_curated)
        self.assertEqual(sorted(self.fight.tags.values_list("name", flat=True)), ["home", "lost"])
        self.rebuild()
        self.assertEqual(self.fight.title, "Hole fight")  # manual titles survive rebuilds

    def test_system(self):
        self.assertFalse(curation.set_system(self.fight, "No Such System", self.user))
        self.assertTrue(curation.set_system(self.fight, "jita", self.user))
        self.fight.refresh_from_db()
        self.assertEqual((self.fight.system.name, self.fight.system_source), ("Jita", "manual"))
        self.rebuild()
        self.assertEqual(self.fight.system.name, "Jita")

    def test_side_override_survives_rebuild(self):
        yagas = list(self.fight.participants.filter(corporation_ticker="YAGAS").values_list("key", flat=True))
        friendly = self.fight.sides.get(order=0)
        curation.set_side(self.fight, yagas, friendly, self.user)
        self.rebuild()
        sides = set(self.fight.participants.filter(corporation_ticker="YAGAS").values_list("side__order", flat=True))
        self.assertEqual(sides, {0})
        self.assertEqual(SideOverride.objects.count(), len(yagas))
        curation.set_side(self.fight, yagas[:1], None, self.user)
        self.rebuild()
        self.assertIsNone(self.fight.participants.get(key=yagas[0]).side)
        curation.rename_side(friendly, "WE", self.user)
        friendly.refresh_from_db()
        self.assertTrue(friendly.name_is_manual)

    def test_attribution_override(self):
        loki = self.fight.participants.filter(kind=Participant.Kind.SHIP_ONLY, key__contains="friendly_act").first()
        self.assertFalse(curation.attribute(self.fight, "p:not-ship-only", "p:Pilot 013", self.user))
        self.assertTrue(curation.attribute(self.fight, loki.key, "p:Pilot 006", self.user))
        self.rebuild()
        loki.refresh_from_db()
        self.assertEqual((loki.attributed_to.name, loki.attribution_source), ("Pilot 006", "manual"))

    def test_exclude_and_include(self):
        log = CombatLog.objects.get()
        self.assertEqual(curation.set_excluded(log, True, "wrong fight", self.user), [self.fight.pk])
        log.refresh_from_db()
        self.assertEqual((log.excluded, log.excluded_reason), (True, "wrong fight"))
        curation.set_excluded(log, False, "", self.user)
        log.refresh_from_db()
        self.assertFalse(log.excluded)
        self.assertEqual(FightChange.objects.filter(action__in=["exclude", "include"]).count(), 2)

    def test_split_and_merge_back(self):
        at = self.fight.start_at + (self.fight.end_at - self.fight.start_at) / 2
        new = curation.split_fight(self.fight, at, self.user)
        self.assertEqual(Engagement.objects.count(), 2)
        self.assertEqual(Fight.objects.filter(is_curated=True).count(), 2)
        merged = curation.merge_fights([self.fight, new], self.user)
        self.assertEqual(Fight.objects.count(), 1)
        self.assertTrue(FightRedirect.objects.filter(fight=merged).exists())

    def test_merge_semantics(self):
        other = Fight.objects.create(
            title="Other",
            start_at=self.fight.start_at,
            end_at=self.fight.end_at,
            notes="from the other fight",
            title_is_manual=True,
            is_curated=True,
        )
        curation.set_tags(other, ["kept"], self.user)
        webhook = DiscordWebhook.objects.create(name="ops", url="https://discord.invalid/x")
        DiscordPost.objects.create(fight=other, webhook=webhook, user=self.user)
        side = other.sides.create(order=0, name="Us", is_friendly=True)
        SideOverride.objects.create(fight=other, participant_key="p:Someone", side=side)
        absorbed_pk = self.fight.pk
        survivor = curation.merge_fights([self.fight, other], self.user)
        self.fight.pk = absorbed_pk
        self.assertEqual(survivor.pk, other.pk)  # the curated one survives
        self.assertIn("from the other fight", survivor.notes)
        self.assertEqual(survivor.title, "Other")
        self.assertEqual(survivor.discord_posts.count(), 1)
        self.assertTrue(survivor.tags.filter(name="kept").exists())
        self.assertTrue(FightRedirect.objects.filter(old_id=self.fight.pk, fight=survivor).exists())

    def test_move_engagement(self):
        target = Fight.objects.create(title="T", start_at=self.fight.start_at, end_at=self.fight.end_at)
        curation.move_engagement(self.fight.engagements.get(), target, self.user)
        self.assertEqual(target.engagements.count(), 1)

    def test_delete_log_and_empty_fights(self):
        log = CombatLog.objects.get()
        stranger = make_user("nobody")
        self.assertFalse(curation.can_delete_log(stranger, log))
        self.assertTrue(curation.can_delete_log(self.user, log))
        curation.delete_log(log, self.user)
        self.assertFalse(is_kept_when_empty(self.fight))
        self.rebuild_deleted()

    def rebuild_deleted(self):
        with offline():
            self.assertIsNone(rebuild_fight(self.fight.pk))
        self.assertFalse(Fight.objects.filter(pk=self.fight.pk).exists())

    def test_curated_empty_fights_are_kept(self):
        curation.set_notes(self.fight, "keep me", self.user)
        curation.delete_log(CombatLog.objects.get(), self.user)
        with offline():
            rebuild_fight(self.fight.pk)
        self.fight.refresh_from_db()
        self.assertEqual(self.fight.summary, {"empty": True})

    def test_can_edit(self):
        self.assertTrue(can_edit(self.user, self.fight))
        member = make_user("member")
        self.assertFalse(can_edit(member, self.fight))


class ZkbTest(TestCase):
    def setUp(self):
        cache.delete(zkb.RETRY_AT_KEY)
        cache.delete(zkb.LAST_REQUEST_KEY)

    def test_zkb_get(self):
        with requests_mock.Mocker() as m:
            m.get("https://zkillboard.com/api/characterID/1/page/1/", json=[{"killmail_id": 1}])
            self.assertEqual(zkb.zkb_get("characterID/1/page/1"), [{"killmail_id": 1}])
            self.assertIn("ffew-fightlog", m.last_request.headers["User-Agent"])
            m.get("https://zkillboard.com/api/x/", status_code=429, headers={"Retry-After": "3"})
            with self.assertRaises(zkb.ZkbRateLimited):
                zkb.zkb_get("x")
            with self.assertRaises(zkb.ZkbRateLimited):
                zkb.zkb_get("characterID/1/page/1")  # still inside the cooldown
            cache.delete(zkb.RETRY_AT_KEY)
            m.get("https://zkillboard.com/api/y/", json={"error": "not a list"})
            self.assertEqual(zkb.zkb_get("y"), [])

    def test_fetch_paths(self):
        start = datetime(2026, 10, 25, 20, 19, tzinfo=dt_timezone.utc)
        paths = []

        def fake(path):
            paths.append(path)
            return [{"killmail_id": 1, "killmail_time": "2026-10-25T19:00:00Z"}] * (200 if "page/1" in path else 0)

        with mock.patch("fightlog.services.zkb.zkb_get", fake):
            zkb.fetch_entity_kills("character", 5, start, start + timedelta(days=1))
            zkb.fetch_entity_kills("alliance", 7, start, start + timedelta(days=30))
        self.assertTrue(paths[0].startswith("characterID/5/pastSeconds/"))
        self.assertTrue(any(p.startswith("allianceID/7/year/2026/month/10") for p in paths))

    def test_save_and_select(self):
        kms = zkill_data()
        km = zkb.save_killmail(kms[0])
        self.assertIsInstance(km, Killmail)
        self.assertIsNone(zkb.save_killmail({"killmail_id": None}))
        participants = {a.get("character_id") for k in kms for a in k["attackers"]} - {None}
        start = datetime(2026, 10, 25, 20, 19, 25, tzinfo=dt_timezone.utc)
        end = datetime(2026, 10, 25, 20, 41, 20, tzinfo=dt_timezone.utc)
        core, related, system = zkb.select_kills(kms, participants, start, end)
        self.assertEqual(system, 31001176)
        self.assertTrue(core)
        self.assertTrue(related)


class DiscordTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner()
        with offline():
            ingest_fixture(cls.user, [SAMPLE_LOG])
        cls.webhook = DiscordWebhook.objects.create(name="ops", url="https://discord.invalid/api/webhooks/1/x")

    def post(self):
        return DiscordPost.objects.create(fight=Fight.objects.get(), webhook=self.webhook, user=self.user)

    @override_settings(SITE_URL="https://auth.example.com")
    def test_message(self):
        message = discord.build_message(Fight.objects.get())
        embed = message["embeds"][0]
        self.assertTrue(embed["url"].startswith("https://auth.example.com/fightlog/fight/"))
        self.assertIn("Top damage", embed["description"])
        self.assertTrue(embed["fields"])
        self.assertEqual(discord._isk(2.5e9), "2.5b")
        self.assertEqual(discord._isk(12), "12")

    def test_send(self):
        with requests_mock.Mocker() as m:
            m.post(self.webhook.url, json={"id": "123"})
            post = discord.send(self.post())
            self.assertEqual((post.status, post.message_id), (DiscordPost.Status.SENT, "123"))
            self.assertEqual(m.last_request.qs, {"wait": ["true"]})
            m.post(self.webhook.url, status_code=429, json={"retry_after": 1.5})
            with self.assertRaises(discord.DiscordRateLimited):
                discord.send(self.post())
            m.post(self.webhook.url, status_code=404, text="Unknown Webhook")
            failed = discord.send(self.post())
            self.assertEqual(failed.status, DiscordPost.Status.FAILED)


class ResolveTest(TestCase):
    def test_names_are_cached(self):
        fake = FakeEsi()
        with fake.patched():
            first = resolve.resolve_names(["Pilot 001", "Nobody At All", ""])
            calls = fake.calls
            again = resolve.resolve_names(["Pilot 001"])
        self.assertEqual(first["Pilot 001"].character_id, 90000001)
        self.assertEqual(first["Pilot 001"].corporation_id, 840323545)
        self.assertIsNone(first["Nobody At All"].character_id)
        self.assertEqual(again["Pilot 001"].character_id, 90000001)
        self.assertEqual(fake.calls, calls)
        self.assertEqual(resolve.resolve_names([]), {})

    def test_failures_are_tolerated(self):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        with mock.patch.multiple(
            "fightlog.services.resolve",
            esi_universe_ids=boom,
            esi_universe_names=boom,
            esi_corporation=boom,
            esi_alliance=boom,
            esi_affiliations=boom,
        ):
            self.assertEqual(resolve.resolve_names(["X"]), {})
            self.assertEqual(resolve.resolve_character_ids([1]), {})
            self.assertEqual(resolve.corporations([1]), {})
            self.assertEqual(resolve.alliances([1]), {})
        with mock.patch("fightlog.services.resolve.esi_affiliations", boom), FakeEsi().patched() as fake:
            fake.affiliations = boom
            self.assertTrue(resolve.resolve_names(["Pilot 006"]))

    def test_ids_corps_alliances(self):
        with FakeEsi().patched():
            names = resolve.resolve_character_ids([90000001, 1])
            self.assertEqual(names, {90000001: "Pilot 001"})
            self.assertEqual(resolve.resolve_character_ids([]), {})
            self.assertEqual(resolve.corporations([840323545])[840323545].ticker, "FFEW")
            self.assertEqual(resolve.alliances([99011699])[99011699].ticker, "WEFEW")
        self.assertTrue(Corporation.objects.exists() and Alliance.objects.exists())
        self.assertTrue(ResolvedName.objects.filter(character_id=90000001).exists())

    def test_systems(self):
        hole = resolve.solar_system(31001176)
        self.assertTrue(hole.is_wspace)
        self.assertTrue(hole.region_name)
        self.assertIsNone(resolve.solar_system(None))
        self.assertIsNone(resolve.solar_system_by_name("Nowhere"))
        with mock.patch.multiple(
            "fightlog.services.resolve",
            esi_system=lambda i: {"name": "New", "security_status": 0.5, "constellation_id": 2},
            esi_constellation=lambda i: {"region_id": 3},
            esi_region=lambda i: {"name": "R"},
        ):
            system = resolve.solar_system(39999999)
        self.assertEqual((system.name, system.region_name), ("New", "R"))
        with mock.patch("fightlog.services.resolve.esi_system", side_effect=RuntimeError):
            self.assertIsNone(resolve.solar_system(39999998))
        self.assertEqual(SolarSystem.objects.count(), 2)

    def test_plain(self):
        class Model:
            def model_dump(self):
                return {"a": 1}

        self.assertEqual(resolve._plain([Model(), {"b": Model()}]), [{"a": 1}, {"b": {"a": 1}}])


__all__ = ["time"]
