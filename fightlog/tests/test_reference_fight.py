"""Golden tests: spec acceptance scenarios 1–3 on the 2026-10-25 reference fight."""

from collections import defaultdict
from datetime import datetime
from datetime import timezone as dt_timezone

from django.test import TestCase

from fightlog.models import CombatLog, Fight, FightKillmail, Participant
from fightlog.services import zkb
from fightlog.services.analysis import analysis, default_active_logs, default_side, load_fight_store, pilot
from fightlog.services.rebuild import rebuild_fight

from .utils import FIGHT_DAY_AFTER, FIVE_LOGS, SAMPLE_LOG, fleet_owner, ingest_fixture, make_user, offline

PYROLANC = {"-G-F", "IMBCN", "LPCR", "NEDX", "NINAC", "OCBU"}


def main_fight() -> Fight:
    return max(Fight.objects.all(), key=lambda f: f.engagements.count())


def corp_sides(fight: Fight) -> dict[str, set]:
    """Corp ticker → set of side orders (None = Unknown) of its pilots."""
    out = defaultdict(set)
    for p in fight.participants.filter(kind=Participant.Kind.PILOT).select_related("side"):
        out[p.corporation_ticker].add(p.side.order if p.side else None)
    return out


class SampleLogTest(TestCase):
    """Scenario 1: the sample log alone."""

    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner()
        with offline():
            ingest_fixture(cls.user, [SAMPLE_LOG])
        cls.fight = Fight.objects.get()

    def test_one_fight_in_the_right_window(self):
        self.assertEqual(Fight.objects.count(), 1)
        self.assertEqual(self.fight.start_at.strftime("%H:%M"), "20:19")
        self.assertTrue("20:4" in self.fight.end_at.strftime("%H:%M"), self.fight.end_at)
        self.assertTrue(self.fight.is_built)

    def test_ffew_is_friendly(self):
        sides = corp_sides(self.fight)
        self.assertEqual(sides["FFEW"], {0})

    def test_targeted_corps_are_hostile(self):
        sides = corp_sides(self.fight)
        for ticker in PYROLANC | {"TRX26", "W-SPG"}:
            self.assertEqual(sides[ticker], {1}, ticker)

    def test_smartbomb_only_corps_are_unknown(self):
        sides = corp_sides(self.fight)
        for ticker in ("YAGAS", "SH-R", "T.-R.", "NPE."):
            self.assertEqual(sides[ticker], {None}, ticker)

    def test_yagas_carries_the_likely_friendly_hint(self):
        hints = set(self.fight.participants.filter(corporation_ticker="YAGAS").values_list("hint", flat=True))
        self.assertEqual(hints, {"aoe_mutual_only"})
        npe = set(self.fight.participants.filter(corporation_ticker="NPE.").values_list("hint", flat=True))
        self.assertEqual(npe, {""})

    def test_reps_from_a_ship_only_loki_count_as_reps_in(self):
        friendly = self.fight.sides.get(order=0)
        result = analysis(self.fight, default_active_logs(self.fight), f"side:{friendly.pk}", 10, False)
        self.assertGreater(sum(result["series"]["rep_in"]), 0)
        loki = self.fight.participants.get(kind=Participant.Kind.SHIP_ONLY, ships="Loki", key__contains="friendly_act")
        self.assertEqual(loki.side, friendly)

    def test_smartbomb_damage_to_missiles_is_left_out(self):
        friendly = self.fight.sides.get(order=0)
        logs = default_active_logs(self.fight)
        without = analysis(self.fight, logs, f"side:{friendly.pk}", 10, False)
        with_non_ships = analysis(self.fight, logs, f"side:{friendly.pk}", 10, True)
        self.assertLess(sum(without["series"]["damage_out"]), sum(with_non_ships["series"]["damage_out"]))
        listener = next(p for p in without["pilots"] if p["name"] == "Pilot 001")
        self.assertGreater(listener["non_ship_out"], 0)

    def test_title_and_system(self):
        self.assertIn("WEFEW vs -PS-", self.fight.title)
        self.assertEqual(self.fight.system_source, Fight.SystemSource.WSPACE_UNKNOWN)


class UnownedSampleLogTest(TestCase):
    """The sample log uploaded for someone nobody in the Auth owns: no friendly seed."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("collector", permissions=["fightlog.basic_access", "fightlog.upload_any_log"])
        with offline():
            ingest_fixture(cls.user, [SAMPLE_LOG])
        cls.fight = Fight.objects.get()

    def test_sides_are_split_without_a_friendly_side(self):
        sides = corp_sides(self.fight)
        self.assertFalse(self.fight.sides.get(order=0).participants.exists())
        (ffew_side,) = sides["FFEW"]
        self.assertIsNotNone(ffew_side)
        for ticker in PYROLANC:
            self.assertEqual(len(sides[ticker]), 1, ticker)
            self.assertNotIn(ffew_side, sides[ticker] | {None, 0}, ticker)

    def test_title_and_default_perspective_skip_the_empty_friendly_side(self):
        self.assertNotIn("Friendly", self.fight.title)
        self.assertIn("WEFEW", self.fight.title)
        side = default_side(self.fight)
        self.assertNotEqual(side.order, 0)
        result = analysis(self.fight, default_active_logs(self.fight), None, 10, False)
        self.assertEqual(result["perspective"], f"side:{side.pk}")
        # Seen from the larger (enemy) side: only smartbombs ever hit this listener, so all
        # the damage is incoming.
        self.assertGreater(sum(result["series"]["damage_in"]), 0)
        self.assertEqual(sum(result["series"]["damage_out"]), 0)


class FiveLogsTest(TestCase):
    """Scenario 2: all five logs of the fight."""

    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner()
        with offline():
            cls.logs, _ = ingest_fixture(cls.user, FIVE_LOGS)
        cls.fight = main_fight()

    def test_one_fight_holds_all_five_logs(self):
        self.assertEqual(self.fight.engagements.values("log").distinct().count(), 5)
        # The Loki pilot's log also has a 20:53 neut skirmish, 12 minutes later: its own fight.
        others = Fight.objects.exclude(pk=self.fight.pk)
        self.assertEqual([f.start_at.strftime("%H:%M") for f in others], ["20:53"])

    def test_hits_between_our_pilots_count_once(self):
        friendly = self.fight.sides.get(order=0)
        by_log = {log.listener_name: log.pk for log in self.logs}
        both = {by_log["Pilot 001"], by_log["Pilot 006"]}
        only_001 = {by_log["Pilot 001"]}
        only_006 = {by_log["Pilot 006"]}

        def ff(logs):
            rows = {p["name"]: p for p in analysis(self.fight, logs, f"side:{friendly.pk}", 10, False)["pilots"]}
            return rows["Pilot 001"]["ff_out"]

        # Pilot 001's smartbombs on Pilot 006 are in both logs; with both active they count once.
        self.assertGreater(ff(only_001), 0)
        self.assertGreater(ff(only_006), 0)
        self.assertLess(ff(both), ff(only_001) + ff(only_006))
        store = load_fight_store(self.fight)
        self.assertGreater(sum(1 for g in store.column("dup_group") if g >= 0), 1000)

    def test_listener_hulls_come_from_other_logs(self):
        ships = dict(self.fight.participants.filter(has_log=True).values_list("name", "ships"))
        self.assertIn("Sleipnir", ships["Pilot 001"])
        self.assertIn("Sleipnir", ships["Pilot 006"])
        self.assertIn("Sleipnir", ships["Pilot 007"])
        self.assertIn("Loki", ships["Pilot 013"])

    def test_clock_offsets_are_close_to_zero(self):
        offsets = set(CombatLog.objects.values_list("clock_offset", flat=True))
        self.assertTrue(all(abs(o) <= 1 for o in offsets), offsets)
        self.assertTrue(self.fight.summary["offset_evidence"])

    def test_paidprogramming_never_gets_the_reps(self):
        loki_reppers = self.fight.participants.filter(
            kind=Participant.Kind.SHIP_ONLY, ships="Loki", key__contains="friendly_act"
        )
        for actor in loki_reppers:
            if actor.attributed_to:
                self.assertNotEqual(actor.attributed_to.name, "Pilot 013")

    def test_pilot_drilldown_adds_up_to_the_pilot_table(self):
        """The weapons in a pilot's drill-down use the table's rules, whatever the view."""
        logs = default_active_logs(self.fight)
        whole = analysis(self.fight, logs, None, 10, False)
        third = (whole["end"] - whole["start"]) // 3
        middle = (whole["start"] + third, whole["end"] - third)
        views = [(None, False, None), (None, True, None), (f"log:{min(logs)}", False, None), (None, False, middle)]
        checked = 0
        for perspective, non_ships, window in views:
            table = analysis(self.fight, logs, perspective, 10, non_ships, window)
            for row in table["pilots"]:
                weapons = pilot(self.fight, logs, row["key"], non_ships, perspective, window)["weapons"]

                def total(field, weapons=weapons):
                    return sum(w[field] for w in weapons)

                label = (perspective, non_ships, window, row["name"])
                self.assertEqual(total("damage"), row["damage_out"], label)
                self.assertEqual(total("hits"), row["hits"], label)
                self.assertEqual(total("misses"), row["misses"], label)
                self.assertEqual(total("ff"), row["ff_out"], label)
                self.assertEqual(total("non_ship"), 0 if non_ships else row["non_ship_out"], label)
                checked += bool(weapons)
        self.assertGreater(checked, 20)
        zoomed = analysis(self.fight, logs, None, 10, False, middle)
        self.assertTrue(middle[0] <= zoomed["start"] <= zoomed["end"] <= middle[1])
        damage = sum(zoomed["series"]["damage_out"])
        self.assertTrue(0 < damage < sum(whole["series"]["damage_out"]), damage)
        listener = next(r for r in analysis(self.fight, logs, None, 10, False)["pilots"] if r["name"] == "Pilot 001")
        weapons = pilot(self.fight, logs, listener["key"])["weapons"]
        self.assertTrue(sum(w["ff"] for w in weapons) and sum(w["non_ship"] for w in weapons))

    def test_uploading_in_another_order_gives_the_same_sides(self):
        before = {k: v for k, v in corp_sides(self.fight).items()}
        Fight.objects.all().delete()
        CombatLog.objects.all().delete()
        with offline():
            ingest_fixture(self.user, list(reversed(FIVE_LOGS)))
        self.assertEqual(corp_sides(main_fight()), before)


class WithKillmailsTest(TestCase):
    """Scenario 3: five logs plus zKillboard."""

    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner()
        with offline(zkb=True):
            ingest_fixture(cls.user, FIVE_LOGS)
            fight = main_fight()
            zkb.link_fight(fight, now=FIGHT_DAY_AFTER)
            fight.data_version += 1
            fight.save()
            rebuild_fight(fight.pk)
        cls.fight = Fight.objects.get(pk=fight.pk)

    def test_yagas_becomes_friendly(self):
        self.assertEqual(corp_sides(self.fight)["YAGAS"], {0})

    def test_smartbomb_only_enemies_become_hostile(self):
        sides = corp_sides(self.fight)
        for ticker in ("SH-R", "T.-R.", "BP RU"):
            self.assertEqual(sides[ticker], {1}, ticker)

    def test_npe_stays_unknown(self):
        self.assertEqual(corp_sides(self.fight)["NPE."], {None})

    def test_system_from_killmails(self):
        self.assertEqual(self.fight.system_id, 31001176)
        self.assertEqual(self.fight.system_source, Fight.SystemSource.ZKB)

    def test_sabre_is_a_related_kill_and_the_omen_is_not_linked(self):
        links = {
            link.killmail.time.strftime("%H:%M:%S"): link.role
            for link in FightKillmail.objects.filter(fight=self.fight).select_related("killmail")
        }
        self.assertEqual(links.get("20:14:34"), "related")
        self.assertNotIn("20:06:46", links)
        self.assertEqual(links.get("20:28:45"), "core")
        # The timeline's death markers name the lost ship.
        kills = analysis(self.fight, default_active_logs(self.fight), None, 10, False)["kills"]
        sabre = next(k for k in kills if k["role"] == "related")
        self.assertEqual(sabre["ship"], "Sabre")
        self.assertTrue(all(k["ship"] and k["url"].startswith("https://zkillboard.com/kill/") for k in kills))

    def test_friendly_fire_capsule_loss(self):
        link = FightKillmail.objects.select_related("killmail", "victim_participant__side").get(
            fight=self.fight, killmail__time=datetime(2026, 10, 25, 20, 32, 4, tzinfo=dt_timezone.utc)
        )
        self.assertEqual(link.victim_participant.side.order, 0)
        self.assertTrue(all(a["aoe_suspect"] for a in link.killmail.attackers))
