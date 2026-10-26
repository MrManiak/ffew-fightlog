"""Analysis core: store, engagements, offsets, dedupe, hulls, sides, attribution, aggregation."""

import random
import time
from collections import Counter
from unittest import TestCase

from fightlog.analysis.aggregate import Perspective, aggregate, drilldown, pilot_breakdown
from fightlog.analysis.attribution import ShipOnlyActor, attribute, parse_ship_only_key
from fightlog.analysis.dedupe import assign_dup_groups
from fightlog.analysis.engagements import build_store, is_pvp, pilot_keys, split_engagements
from fightlog.analysis.hulls import HullTimeline
from fightlog.analysis.identity import FRIENDLY_ACT, HOSTILE_ACT, actor_dict, actor_key, display_name, polarity
from fightlog.analysis.offsets import estimate_offsets, pair_offset
from fightlog.analysis.sides import KillmailEvidence, Party, collect_interactions, guess_sides
from fightlog.analysis.store import FIGHT_COLUMNS, BlobTooLarge, EventStore
from fightlog.parser import Actor, ActorKind, Event, Flag, Kind, Quality

LISTENER = Actor(ActorKind.LISTENER, name="Me", raw="you")


def pilot(name, corp="AAA", ship="Rifter"):
    return Actor(ActorKind.PILOT, name=name, ship=ship, corp=corp, raw=f"{name}[{corp}]({ship})")


def ev(t, kind=Kind.DAMAGE, src=LISTENER, tgt=None, amount=100, module="Gun", flags=Flag.CAN_MISS):
    return Event(t, kind, src, tgt or pilot("Enemy"), module, amount, Quality.HITS, flags, t)


def fight_store(rows, actors):
    """rows: (t, kind, src_key, tgt_key, module, amount, flags, log_id)."""
    store = EventStore(FIGHT_COLUMNS)
    index = {a["key"]: a for a in actors}
    for t, kind, src, tgt, module, amount, flags, log_id in rows:
        store.append(
            t=t,
            kind=int(kind),
            src=store.actor_id(index[src]),
            tgt=store.actor_id(index[tgt]),
            module=store.module_id(module),
            amount=amount,
            quality=3,
            flags=int(flags),
            line_no=t,
            log_id=log_id,
            dup_group=-1,
        )
    return store


class StoreTest(TestCase):
    def test_roundtrip_and_slice(self):
        store = EventStore(meta={"hulls": [["p:A", "Loki", 1, 2]]})
        a = store.actor_id({"key": "p:A", "kind": "pilot"})
        self.assertEqual(store.actor_id({"key": "p:A"}), a)
        store.append(t=10, kind=0, src=a, tgt=a, module=store.module_id("Gun"), amount=5, quality=3, flags=1, line_no=1)
        store.append(t=11, kind=1, src=a, tgt=a, module=store.module_id(None), amount=0, quality=0, flags=0, line_no=2)
        decoded = EventStore.decode(store.encode())
        self.assertEqual(list(decoded.rows()), list(store.rows()))
        self.assertEqual(decoded.meta, store.meta)
        self.assertEqual(decoded.module_name(0), "Gun")
        self.assertIsNone(decoded.module_name(-1))
        self.assertEqual(len(decoded.slice(1, 2)), 1)

    def test_blob_limit(self):
        store = EventStore()
        for i in range(2000):
            store.append(
                t=random.randrange(10**9),
                kind=0,
                src=0,
                tgt=0,
                module=-1,
                amount=random.randrange(10**6),
                quality=0,
                flags=0,
                line_no=i,
            )
        with self.assertRaises(BlobTooLarge):
            store.encode(max_bytes=100)


class IdentityTest(TestCase):
    def test_keys(self):
        self.assertEqual(actor_key(LISTENER, 1, HOSTILE_ACT), "p:Me")
        self.assertEqual(actor_key(pilot("A"), 1, HOSTILE_ACT), "p:A")
        self.assertEqual(
            actor_key(Actor(ActorKind.SHIP_ONLY, ship="Loki", raw="Loki"), 7, FRIENDLY_ACT), "s:7:friendly_act:Loki"
        )
        self.assertEqual(actor_key(Actor(ActorKind.DRONE, name="Warrior II", owner="Bob"), 1, HOSTILE_ACT), "p:Bob")
        self.assertEqual(actor_key(Actor(ActorKind.DRONE, name="Hornet", corp="X"), 1, HOSTILE_ACT), "d:X:Hornet")
        self.assertEqual(actor_key(Actor(ActorKind.NPC, name="Rat"), 1, HOSTILE_ACT), "n:Rat")
        self.assertEqual(actor_key(Actor(ActorKind.STRUCTURE, name="Keep"), 1, HOSTILE_ACT), "st:Keep")
        self.assertEqual(actor_key(Actor(ActorKind.CHARGE, name="HAM", corp="X"), 1, HOSTILE_ACT), "c:X:HAM")
        self.assertEqual(actor_key(Actor(ActorKind.EMPTY), 1, HOSTILE_ACT), "e:")
        self.assertEqual(actor_key(Actor(ActorKind.UNKNOWN, name="?x"), 1, HOSTILE_ACT), "u:?x")
        self.assertEqual(polarity(ev(1, Kind.REP_SHIELD)), FRIENDLY_ACT)
        self.assertEqual(polarity(ev(1, flags=Flag.OBSERVED)), "observed")

    def test_actor_dict_and_display(self):
        d = actor_dict(Actor(ActorKind.DRONE, name="Warrior II", owner="Bob", raw="w"), "p:Bob")
        self.assertEqual((d["kind"], d["name"]), ("pilot", "Bob"))
        self.assertNotIn("ship", d)
        listener = actor_dict(LISTENER, "p:Me", "Me")
        self.assertTrue(listener["listener"])
        self.assertEqual(display_name({"key": "s:1:x:Loki", "kind": "ship_only", "ship": "Loki"}), "Unknown Loki")
        self.assertEqual(display_name({"key": "k"}), "k")


class EngagementTest(TestCase):
    def test_pvp_filter_and_gap_split(self):
        npc = Actor(ActorKind.NPC, name="Rat")
        events = [ev(t) for t in range(0, 10)] + [ev(20, tgt=npc)] + [ev(t) for t in range(1000, 1004)]
        self.assertFalse(is_pvp(events[10]))
        groups = split_engagements(events, gap_seconds=300, min_events=5)
        self.assertEqual([len(g) for g in groups], [10])
        observed = ev(
            5,
            src=Actor(ActorKind.SHIP_ONLY, ship="Crow", raw="Crow"),
            tgt=Actor(ActorKind.SHIP_ONLY, ship="Loki", raw="Loki"),
            flags=Flag.OBSERVED,
        )
        self.assertTrue(is_pvp(observed))

    def test_store_records_hulls_and_pilot_keys(self):
        events = [ev(1, tgt=pilot("A", ship="Loki")), ev(9, tgt=pilot("A", ship="Loki")), ev(5, tgt=pilot("B"))]
        store = build_store(events, log_id=3, listener_name="Me")
        self.assertEqual(pilot_keys(store), ["p:A", "p:B", "p:Me"])
        self.assertIn(["p:A", "Loki", 1, 9], store.meta["hulls"])


class OffsetsTest(TestCase):
    def test_pair_offset(self):
        a = [(t, ("sig", t % 7)) for t in range(100, 160)]
        b = [(t + 3, sig) for t, sig in a]
        self.assertEqual(pair_offset(a, b, 10), (3, 60))
        self.assertEqual(pair_offset(a, [], 10), (0, 0))

    def test_estimate_offsets(self):
        actors = [{"key": "p:A", "kind": "pilot"}, {"key": "p:B", "kind": "pilot"}]
        rows = []
        for i, t in enumerate(range(1000, 1060, 2)):
            rows.append((t, Kind.DAMAGE, "p:A", "p:B", "Gun", 100 + i, 0, 1))
            rows.append((t + 4, Kind.DAMAGE, "p:A", "p:B", "Gun", 100 + i, 0, 2))
        store = fight_store(sorted(rows), actors)
        offsets, evidence = estimate_offsets(store, {1: "p:A", 2: "p:B"}, 10)
        self.assertEqual(offsets[2] - offsets[1], 4)
        self.assertTrue(evidence)
        self.assertEqual(estimate_offsets(store, {1: "p:A"}, 10)[0], {1: 0})


class DedupeTest(TestCase):
    def setUp(self):
        self.actors = [
            {"key": "p:A", "kind": "pilot"},
            {"key": "p:B", "kind": "pilot"},
            {"key": "s:2:friendly_act:Loki", "kind": "ship_only", "ship": "Loki", "raw": "Loki"},
            {"key": "s:1:observed:Nighthawk", "kind": "ship_only", "ship": "Nighthawk", "raw": "Nighthawk"},
            {"key": "s:1:observed:Sleipnir", "kind": "ship_only", "ship": "Sleipnir", "raw": "Sleipnir"},
            {"key": "s:2:hostile_act:Nighthawk", "kind": "ship_only", "ship": "Nighthawk", "raw": "Nighthawk"},
            {"key": "s:3:observed:Nighthawk", "kind": "ship_only", "ship": "Nighthawk", "raw": "Nighthawk"},
            {"key": "s:3:observed:Sleipnir", "kind": "ship_only", "ship": "Sleipnir", "raw": "Sleipnir"},
        ]

    def test_direct_ship_only_and_observed_matches(self):
        rows = [
            (100, Kind.DAMAGE, "p:A", "p:B", "Gun", 50, 0, 1),  # A's log
            (101, Kind.DAMAGE, "p:A", "p:B", "Gun", 50, 0, 2),  # B's log, 1 s later
            (200, Kind.REP_SHIELD, "p:A", "p:B", "Booster", 250, 0, 1),
            (200, Kind.REP_SHIELD, "s:2:friendly_act:Loki", "p:B", "Booster", 250, 0, 2),
            (300, Kind.SCRAM, "s:1:observed:Nighthawk", "s:1:observed:Sleipnir", None, 0, Flag.OBSERVED, 1),
            (300, Kind.SCRAM, "s:2:hostile_act:Nighthawk", "p:B", None, 0, 0, 2),
            (300, Kind.SCRAM, "s:3:observed:Nighthawk", "s:3:observed:Sleipnir", None, 0, Flag.OBSERVED, 3),
            (400, Kind.DAMAGE, "p:A", "p:B", "Gun", 99, 0, 1),  # no copy in B's log
        ]
        store = fight_store(rows, self.actors)
        hulls = HullTimeline()
        hulls.add("p:A", "Loki", 0, 500, "peer_log")
        hulls.add("p:B", "Sleipnir", 0, 500, "peer_log")
        groups, attributions = assign_dup_groups(store, {1: "p:A", 2: "p:B", 3: "p:C"}, hulls, 2)
        dup = list(store.column("dup_group"))
        self.assertEqual(dup[0], dup[1])
        self.assertEqual(dup[2], dup[3])
        self.assertTrue(dup[4] == dup[5] == dup[6] >= 0)
        self.assertEqual(dup[7], -1)
        self.assertEqual(attributions["s:2:friendly_act:Loki"]["p:A"], 1)
        self.assertEqual(attributions["s:1:observed:Sleipnir"]["p:B"], 1)
        self.assertGreaterEqual(groups, 3)


class HullsTest(TestCase):
    def test_timeline(self):
        hulls = HullTimeline()
        hulls.add("p:A", "Porpoise", 0, 100, "peer_log")
        hulls.add("p:A", "Sleipnir", 3000, 4000, "peer_log")
        hulls.add("p:A", "Sleipnir", 4500, 4500, "killmail")
        hulls.add("p:A", "", 1, 1, "peer_log")
        self.assertEqual(hulls.at("p:A", 50), "Porpoise")
        self.assertEqual(hulls.at("p:A", 4200), "Sleipnir")  # bracketed by the same ship
        self.assertEqual(hulls.at("p:A", 550), "Porpoise")  # nearest observation within 15 min
        self.assertEqual(hulls.at("p:A", 2800), "Sleipnir")
        self.assertIsNone(hulls.at("p:A", 1500))  # nothing close, and two different ships
        self.assertIsNone(hulls.at("p:X", 1))
        self.assertTrue(hulls.compatible("p:X", "Loki", 1))
        self.assertTrue(hulls.compatible("p:A", None, 1))
        self.assertFalse(hulls.compatible("p:A", "Loki", 50))
        self.assertTrue(hulls.flew("p:A", "Porpoise"))
        self.assertEqual([o.ship for o in hulls.intervals("p:A")], ["Porpoise", "Sleipnir"])
        self.assertEqual(hulls.intervals("p:A")[1].last, 4500)
        self.assertIn("p:A", hulls.keys())
        single = HullTimeline()
        single.add("p:B", "Loki", 100, 100, "killmail")
        self.assertEqual(single.at("p:B", 99999), "Loki")


class SidesTest(TestCase):
    def parties(self):
        return {
            "p:us1": Party("p:us1", corp_ticker="US", aa_owned=True),
            "p:us2": Party("p:us2", corp_ticker="US"),
            "p:ally": Party("p:ally", corp_ticker="ALLY"),
            "p:foe1": Party("p:foe1", corp_ticker="FOE", alliance_id=7),
            "p:foe2": Party("p:foe2", corp_ticker="FOE2", alliance_id=7),
            "p:third": Party("p:third", corp_ticker="TRD"),
            "p:quiet": Party("p:quiet", corp_ticker="QT"),
        }

    def test_aoe_is_not_evidence_and_hostiles_group_by_alliance(self):
        hostile = Counter({("p:us1", "p:foe1"): 10, ("p:foe2", "p:us2"): 5})
        aoe = Counter({("p:us1", "p:ally"): 30, ("p:ally", "p:us1"): 30, ("p:us1", "p:quiet"): 9})
        result = guess_sides(self.parties(), hostile, Counter(), aoe, [], [], set())
        self.assertEqual(result.side_of("p:us2"), 0)
        self.assertEqual(result.side_of("p:foe1"), 1)
        self.assertEqual(result.side_of("p:foe2"), 1)
        self.assertIsNone(result.side_of("p:ally"))
        self.assertEqual(result.hints[result.group_of["p:ally"]], "aoe_mutual_only")
        self.assertNotIn(result.group_of["p:quiet"], result.hints)

    def test_reps_fleet_killmails_and_third_parties(self):
        friendly = Counter({("p:ally", "p:us1"): 3})
        hostile = Counter({("p:us1", "p:foe1"): 10})
        killmails = [
            KillmailEvidence("p:ally", [("p:foe1", 0.9, False), ("p:us1", 0.1, True)]),
            KillmailEvidence("p:third", [("p:foe1", 0.5, False), ("p:us1", 0.5, False)]),
            KillmailEvidence("p:foe1", [("p:third", 1.0, False)]),
            KillmailEvidence("p:foe2", [("p:third", 1.0, False)]),
        ]
        result = guess_sides(self.parties(), hostile, friendly, Counter(), [("p:us1", "p:quiet")], killmails, set())
        self.assertEqual(result.side_of("p:ally"), 0)
        self.assertEqual(result.side_of("p:quiet"), 0)
        self.assertEqual(result.side_of("p:foe1"), 1)
        self.assertNotIn(result.side_of("p:third"), (None, 0, 1))

    def test_seeds_pins_and_low_confidence(self):
        parties = self.parties()
        parties["p:ally"] = Party("p:ally", corp_id=55)
        hostile = Counter({("p:us1", "p:foe1"): 2, ("p:us1", "p:third"): 2, ("p:ally", "p:third"): 2})
        friendly = Counter({("p:third", "p:us1"): 2})
        result = guess_sides(parties, hostile, friendly, Counter(), [], [], {"c:55"}, pinned={"p:quiet": 1})
        self.assertEqual(result.side_of("p:ally"), 0)
        self.assertEqual(result.side_of("p:quiet"), 1)
        self.assertIsNone(result.side_of("p:foe1"))  # too little evidence
        self.assertIsNone(result.side_of("p:nobody"))

    def test_without_seeds_the_busiest_group_anchors_side_one(self):
        parties = {key: party for key, party in self.parties().items() if key != "p:us1"}
        hostile = Counter({("p:us2", "p:foe1"): 10, ("p:foe2", "p:us2"): 6, ("p:ally", "p:foe1"): 4})
        friendly = Counter({("p:ally", "p:us2"): 3})
        result = guess_sides(parties, hostile, friendly, Counter(), [], [], set())
        self.assertEqual(result.friendly_groups, set())
        self.assertEqual(result.side_of("p:foe1"), result.side_of("p:foe2"))
        self.assertEqual(result.side_of("p:us2"), result.side_of("p:ally"))
        self.assertNotEqual(result.side_of("p:us2"), result.side_of("p:foe1"))
        self.assertEqual({result.side_of("p:us2"), result.side_of("p:foe1")}, {1, 2})
        self.assertIsNone(result.side_of("p:quiet"))
        empty = guess_sides(parties, Counter({("p:us2", "p:foe1"): 2}), Counter(), Counter(), [], [], set())
        self.assertEqual(empty.side_of_group, {})

    def test_collect_interactions_counts_dup_groups_once(self):
        actors = [{"key": "p:A"}, {"key": "p:B"}, {"key": "n:Rat"}]
        rows = [
            (1, Kind.DAMAGE, "p:A", "p:B", "Gun", 1, 0, 1),
            (1, Kind.DAMAGE, "p:A", "p:B", "Gun", 1, 0, 2),
            (2, Kind.DAMAGE, "p:A", "p:B", "Bomb", 1, Flag.AOE, 1),
            (3, Kind.REP_ARMOR, "p:B", "p:A", "Rep", 1, 0, 1),
            (4, Kind.DAMAGE, "p:A", "n:Rat", "Gun", 1, 0, 1),
            (5, Kind.MISS, "p:A", "p:B", "Gun", 0, 0, 1),
        ]
        store = fight_store(rows, actors)
        store.column("dup_group")[0] = store.column("dup_group")[1] = 0
        hostile, friendly, aoe = collect_interactions(store, {"p:A", "p:B"})
        self.assertEqual(hostile[("p:A", "p:B")], 1)
        self.assertEqual(friendly[("p:B", "p:A")], 1)
        self.assertEqual(aoe[("p:A", "p:B")], 1)


class AttributionTest(TestCase):
    def test_peer_match_elimination_override(self):
        hulls = HullTimeline()
        hulls.add("p:logged", "Loki", 0, 1000, "peer_log")
        hulls.add("p:other", "Loki", 0, 1000, "killmail")
        hulls.add("p:enemy", "Loki", 0, 1000, "peer_log")
        side_of = {"p:me": 0, "p:logged": 0, "p:other": 0, "p:enemy": 1}
        actors = [
            ShipOnlyActor("s:1:friendly_act:Loki", 1, FRIENDLY_ACT, "Loki", [100, 200], "p:me"),
            ShipOnlyActor("s:1:friendly_act:Matched", 1, FRIENDLY_ACT, "Loki", [100], "p:me"),
            ShipOnlyActor("s:1:hostile_act:Loki", 1, HOSTILE_ACT, "Loki", [100], "p:me"),
            ShipOnlyActor("s:1:observed:Loki", 1, "observed", "Loki", [100], "p:me"),
            ShipOnlyActor("s:1:friendly_act:Pinned", 1, FRIENDLY_ACT, "Loki", [100], "p:me"),
        ]
        result = attribute(
            actors,
            {"s:1:friendly_act:Matched": Counter({"p:logged": 3})},
            side_of,
            {"p:me", "p:logged", "p:other", "p:enemy"},
            hulls,
            {"p:logged": (0, 1000)},
            {"s:1:friendly_act:Pinned": "p:me"},
        )
        self.assertEqual(result["s:1:friendly_act:Loki"], ("p:other", "elimination"))
        self.assertEqual(result["s:1:friendly_act:Matched"], ("p:logged", "peer_log"))
        self.assertEqual(result["s:1:hostile_act:Loki"], ("p:enemy", "elimination"))
        self.assertNotIn("s:1:observed:Loki", result)
        self.assertEqual(result["s:1:friendly_act:Pinned"], ("p:me", "manual"))
        self.assertEqual(parse_ship_only_key("s:3:friendly_act:Loki [x]"), (3, "friendly_act", "Loki [x]"))
        self.assertIsNone(parse_ship_only_key("p:A"))
        self.assertIsNone(parse_ship_only_key("s:bad"))


class AggregateTest(TestCase):
    def setUp(self):
        self.actors = [
            {"key": "p:us", "kind": "pilot"},
            {"key": "p:mate", "kind": "pilot"},
            {"key": "p:foe", "kind": "pilot"},
            {"key": "s:1:friendly_act:Loki", "kind": "ship_only", "ship": "Loki"},
            {"key": "c:X:HAM", "kind": "charge"},
            {"key": "n:Rat", "kind": "npc"},
        ]
        rows = [
            (100, Kind.DAMAGE, "p:us", "p:foe", "Gun", 500, Flag.CAN_MISS, 1),
            (101, Kind.MISS, "p:us", "p:foe", "Gun", 0, Flag.CAN_MISS, 1),
            (102, Kind.DAMAGE, "p:foe", "p:us", "HAM", 300, 0, 1),
            (103, Kind.DAMAGE, "p:us", "p:mate", "Bomb", 30, Flag.AOE, 1),
            (104, Kind.REP_SHIELD, "s:1:friendly_act:Loki", "p:us", "Booster", 250, 0, 1),
            (105, Kind.CAP, "p:mate", "p:us", "Cap", 100, 0, 2),
            (106, Kind.NEUT, "p:foe", "p:us", "Neut", 60, 0, 1),
            (107, Kind.NOS, "p:us", "p:foe", "Nos", 5, 0, 1),
            (108, Kind.JAM, "p:foe", "p:mate", "ECM", 0, 0, 2),
            (109, Kind.SCRAM, "p:us", "p:foe", None, 0, 0, 1),
            (110, Kind.DAMAGE, "p:us", "c:X:HAM", "Bomb", 900, Flag.NON_SHIP_TARGET, 1),
            (111, Kind.DAMAGE, "p:us", "n:Rat", "Gun", 10, 0, 1),
            (112, Kind.DAMAGE, "p:us", "p:foe", "Gun", 500, Flag.CAN_MISS, 2),
        ]
        self.store = fight_store(rows, self.actors)
        self.store.column("dup_group")[0] = self.store.column("dup_group")[12] = 0
        self.participants = {
            "p:us": {"name": "Us", "kind": "pilot", "side": 10, "has_log": True},
            "p:mate": {"name": "Mate", "kind": "pilot", "side": 10, "has_log": True},
            "p:foe": {"name": "Foe", "kind": "pilot", "side": 20, "has_log": False},
            "s:1:friendly_act:Loki": {"name": "Unknown Loki", "kind": "ship_only", "side": 10, "alias": "p:mate"},
            "c:X:HAM": {"name": "HAM", "kind": "charge", "party": False},
            "n:Rat": {"name": "Rat", "kind": "npc", "party": False},
            "__log:1": {"key": "p:us", "party": False},
        }

    def test_side_perspective(self):
        result = aggregate(
            self.store,
            self.participants,
            {1, 2},
            Perspective("side", "10"),
            10,
            kills=[{"victim_key": "p:foe", "time": 120}],
        )
        rows = {r["key"]: r for r in result["pilots"]}
        self.assertEqual(rows["p:us"]["damage_out"], 500)  # the duplicate counts once
        self.assertEqual(rows["p:us"]["hits"], 1)
        self.assertEqual(rows["p:us"]["misses"], 1)
        self.assertEqual(rows["p:us"]["hit_pct"], 50.0)
        self.assertEqual(rows["p:us"]["ff_out"], 30)
        self.assertEqual(rows["p:us"]["non_ship_out"], 900)
        self.assertEqual(rows["p:mate"]["rep_out"]["shield"], 250)  # the Loki was Mate
        self.assertEqual(rows["p:us"]["cap_in"], 100)
        self.assertEqual(rows["p:us"]["neut_in"], 60)
        self.assertEqual(rows["p:mate"]["jam_in"], 1)
        self.assertEqual(rows["p:us"]["tackle_out"], 1)
        self.assertEqual(rows["p:foe"]["states"]["default"], "partial")
        self.assertEqual(rows["p:us"]["states"]["default"], "complete")
        self.assertEqual(rows["p:mate"]["states"]["hit_pct"], "na")
        self.assertEqual(sum(result["series"]["damage_out"]), 500)
        self.assertEqual(sum(result["series"]["damage_in"]), 300)
        self.assertEqual(sum(result["series"]["friendly_fire"]), 30)
        target = next(t for t in result["targets"] if t["key"] == "p:foe")
        self.assertTrue(target["died"])
        self.assertEqual(result["totals"]["10"]["damage_out"], 500)
        self.assertNotIn("n:Rat", rows)

    def test_non_ships_logs_and_perspectives(self):
        with_non = aggregate(
            self.store, self.participants, {1, 2}, Perspective("side", "10"), 10, include_non_ships=True
        )
        self.assertEqual({r["key"]: r for r in with_non["pilots"]}["p:us"]["damage_out"], 1400)
        only_two = aggregate(self.store, self.participants, {2}, Perspective("pilot", "p:us"), 5)
        self.assertEqual(sum(only_two["series"]["damage_out"]), 500)
        log_view = aggregate(self.store, self.participants, {1, 2}, Perspective("log", "1"), 10)
        self.assertEqual(sum(log_view["series"]["damage_out"]), 500)
        empty = aggregate(self.store, self.participants, set(), Perspective.parse(None, "10"), 10)
        self.assertEqual(empty["labels"], [])
        self.assertEqual(str(Perspective.parse("bogus", "3")), "side:3")

    def test_drilldown_and_breakdown(self):
        dd = drilldown(self.store, self.participants, {1, 2}, 100, 112, key="p:us", limit=3, log_names={1: "us"})
        self.assertEqual(dd["total"], 11)
        self.assertEqual(len(dd["events"]), 3)
        self.assertEqual(dd["events"][0]["log"], "us")
        kinds = drilldown(self.store, self.participants, {1}, 0, 999, kinds={int(Kind.REP_SHIELD)})
        self.assertEqual(kinds["events"][0]["src"], "Mate")
        breakdown = pilot_breakdown(self.store, self.participants, {1, 2}, "p:us")
        self.assertEqual(breakdown["weapons"][0]["name"], "Bomb")
        self.assertEqual(breakdown["repped_by"][0]["name"], "Mate")
        self.assertTrue(breakdown["ewar_by"])
        self.assertEqual(breakdown["damaged_by"][0]["name"], "Foe")


class PerformanceTest(TestCase):
    """Technical plan §5.5: 250k events, 45 of 50 logs active, well under 2 s."""

    def test_250k_events(self):
        random.seed(7)
        actors = [{"key": f"p:{i}", "kind": "pilot"} for i in range(300)]
        participants = {
            a["key"]: {"name": a["key"], "kind": "pilot", "side": i % 3, "has_log": i < 50}
            for i, a in enumerate(actors)
        }
        store = EventStore(FIGHT_COLUMNS, actors, ["Gun", "Bomb", "Rep"])
        kinds = [0, 0, 0, 0, 1, 2, 3, 6, 8, 9]
        for i in range(250_000):
            store.append(
                t=1000 + i // 140,
                kind=random.choice(kinds),
                src=random.randrange(300),
                tgt=random.randrange(300),
                module=random.randrange(3),
                amount=random.randrange(1, 3000),
                quality=3,
                flags=random.choice((0, 0, 0, 1, 16)),
                line_no=i,
                log_id=random.randrange(50),
                dup_group=(i // 2) if random.random() < 0.3 else -1,
            )
        start = time.perf_counter()
        result = aggregate(store, participants, set(range(5, 50)), Perspective("side", "0"), 10)
        elapsed = time.perf_counter() - start
        self.assertTrue(result["pilots"])
        self.assertLess(elapsed, 2.0, f"aggregation took {elapsed:.2f}s")
        blob = store.encode()
        start = time.perf_counter()
        EventStore.decode(blob)
        self.assertLess(time.perf_counter() - start, 1.0)
