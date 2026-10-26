"""Parser: markup, labels, line formats, classification, context and the corpus shapes."""

import gzip
from unittest import TestCase

from fightlog.catalog import load_catalog
from fightlog.parser import ActorKind, ContextKind, Flag, Kind, Quality, parse_filename, parse_log, read_header
from fightlog.parser.labels import LabelTemplate, parse_damage_label, parse_overview_label, tokenize_overview
from fightlog.parser.markup import clean, leading_color, normalize_ws, showinfo_links
from fightlog.parser.patterns import match_combat, skeleton, split_damage_body, split_last_dash, split_tackle

from .utils import FIXTURES

CATALOG = load_catalog()
HEADER = (
    "------------------------------------------------------------\n"
    "  Gamelog\n  Listener: Pilot Listener\n  Session Started: 2026.10.25 19:51:45\n"
    "------------------------------------------------------------\n"
)

DMG_OUT = (
    "<color=0xff00ffff><b>{n}</b> <color=0x77ffffff><font size=10>to</font> <b><color=0xffffffff>{label}</b>"
    "<font size=10><color=0x77ffffff> - {weapon} - {q}"
)
DMG_IN = (
    "<color=0xffcc0000><b>{n}</b> <color=0x77ffffff><font size=10>from</font> <b><color=0xffffffff>{label}</b>"
    "<font size=10><color=0x77ffffff> - {weapon} - {q}"
)
REP_IN = (
    "<color=0xffccff66><b>{n}</b><color=0x77ffffff><font size=10> remote shield boosted by </font><b>"
    "<color=0xffffffff><font size=12><color=0xFFFFB900> <u><b>{label}</b></u></color></font> </b>"
    "<color=0x77ffffff><font size=10> - {module}</font>"
)
NEUT = (
    "<color={color}><b>{n} GJ</b><color=0x77ffffff><font size=10> energy neutralized </font><b>"
    "<color=0xffffffff><font size=12><color=0xFFFFB900> <u><b>{label}</b></u></color></font> </b>"
    "<color=0x77ffffff><font size=10> - {module}</font>"
)
TACKLE = (
    "<color=0xffffffff><b>Warp scramble attempt</b> <color=0x77ffffff><font size=10>from</font> "
    "<color=0xffffffff><b><font size=12><color=0xFFFFB900> <u><b>{src}</b></u></color></font> </b> "
    "<color=0x77ffffff><font size=10>to <b><color=0xffffffff></font>{tgt}"
)


def log_text(*messages, kind="combat", start="2026.10.25 20:19:"):
    lines = [HEADER]
    for i, msg in enumerate(messages):
        if isinstance(msg, tuple):
            kind_i, msg = msg
        else:
            kind_i = kind
        lines.append(f"[ {start}{i % 60:02d} ] ({kind_i}) {msg}\n")
    return "".join(lines).encode("utf-8")


class MarkupTest(TestCase):
    def test_clean_strips_tags_then_decodes_entities(self):
        msg = "<b>Loki &lt;SPORK&gt;[SLAPD] PilotCharlie </b><font size=10>to</font> you!"
        self.assertEqual(clean(msg), "Loki <SPORK>[SLAPD] PilotCharlie to you!")

    def test_whitespace_inside_brackets_survives(self):
        self.assertEqual(normalize_ws("Rifter   [J  H]   pilotdelta"), "Rifter [J  H] pilotdelta")

    def test_leading_color_and_showinfo(self):
        msg = '<color=0xFF7FFFFF>x <a href="showinfo:1379//2120273592">Pilot A</a> wants you'
        self.assertEqual(leading_color(msg), "0xff7fffff")
        self.assertEqual(showinfo_links(msg), [(1379, 2120273592, "Pilot A")])
        self.assertIsNone(leading_color("plain"))


class DamageLabelTest(TestCase):
    def test_pilot(self):
        a = parse_damage_label("Pilot Kilo I[IMBCN](Cenotaph)", CATALOG)
        self.assertEqual((a.kind, a.name, a.corp, a.ship), (ActorKind.PILOT, "Pilot Kilo I", "IMBCN", "Cenotaph"))

    def test_capsule_with_dashes(self):
        a = parse_damage_label("Pilot Lima[GANCN](Capsule - Genolution 'Auroral' 197-variant)", CATALOG)
        self.assertEqual(a.kind, ActorKind.PILOT)

    def test_charge_drone_structure(self):
        self.assertEqual(
            parse_damage_label(
                "Advanced Heavy Assault Missile[IMBCN](Scourge Rage Heavy Assault Missile)", CATALOG
            ).kind,
            ActorKind.CHARGE,
        )
        self.assertEqual(parse_damage_label("Hornet EC-N[IMBCN](Hornet EC-N)", CATALOG).kind, ActorKind.DRONE)
        self.assertEqual(parse_damage_label("Warrior II[X](Warrior II)", CATALOG).kind, ActorKind.DRONE)
        s = parse_damage_label("KZFV-4 » RNM-Y6 - Our Bridge[SL0W.](Ansiblex Jump Gate)", CATALOG)
        self.assertEqual((s.kind, s.name), (ActorKind.STRUCTURE, "KZFV-4 » RNM-Y6 - Our Bridge"))
        c = parse_damage_label("Customs Office (Deltole X) [Sons of Black Rise][SOBR.](Customs Office)", CATALOG)
        self.assertEqual((c.kind, c.corp), (ActorKind.STRUCTURE, "SOBR."))

    def test_unknown_type_with_other_name_is_a_pilot_in_a_new_hull(self):
        self.assertEqual(parse_damage_label("Someone[ABC](Brand New Hull)", CATALOG).kind, ActorKind.PILOT)

    def test_bare(self):
        a = parse_damage_label("Centii Plague", CATALOG)
        self.assertEqual((a.kind, a.name), (ActorKind.UNKNOWN, "Centii Plague"))


class OverviewLabelTest(TestCase):
    def parse(self, text, template=None):
        return parse_overview_label(tokenize_overview(text, CATALOG), CATALOG, template)

    def test_ship_only(self):
        a = self.parse("Loki")
        self.assertEqual((a.kind, a.ship), (ActorKind.SHIP_ONLY, "Loki"))

    def test_ship_and_bracketed_pilot(self):
        a = self.parse("Ikitursa [Pilot Alfa]")
        self.assertEqual((a.kind, a.name, a.ship), (ActorKind.PILOT, "Pilot Alfa", "Ikitursa"))

    def test_alliance_corp_pilot_with_trailing_dash(self):
        a = self.parse("Augoror [PS] [SLUP] [Pilot Bravo Two] -")
        self.assertEqual((a.name, a.corp, a.alliance), ("Pilot Bravo Two", "SLUP", "PS"))

    def test_one_ticker_defaults_to_corp_and_can_be_learned(self):
        self.assertEqual(self.parse("Basilisk [VI.TA] [PaidProgramming]").corp, "VI.TA")
        template = LabelTemplate()
        template.learn(tokenize_overview("Basilisk [WEFEW] [Pilot A]", CATALOG), {"Pilot A": "FFEW"})
        template.settle()
        a = self.parse("Basilisk [WEFEW] [Pilot B]", template)
        self.assertEqual((a.corp, a.alliance), (None, "WEFEW"))

    def test_stock_label(self):
        a = self.parse("Loki <SPORK>[SLAPD] PilotCharlie")
        self.assertEqual((a.name, a.corp, a.alliance, a.ship), ("PilotCharlie", "SLAPD", "SPORK", "Loki"))
        b = self.parse("Rifter [J  H] pilotdelta")
        self.assertEqual((b.name, b.corp), ("pilotdelta", "J  H"))

    def test_full_label_with_ship_name(self):
        a = self.parse("Brutix Pilotecho98 (HIREV) [-ENIE] - “Shipname”")
        self.assertEqual((a.name, a.corp, a.alliance, a.ship), ("Pilotecho98", "HIREV", "-ENIE", "Brutix"))

    def test_drones_and_structures(self):
        d = self.parse("Acolyte II [BCAUS] [KS88] [Acolyte II] -")
        self.assertEqual((d.kind, d.corp), (ActorKind.DRONE, "KS88"))
        g = self.parse("Garde II Garde II (VNTLO)")
        self.assertEqual((g.kind, g.corp), (ActorKind.DRONE, "VNTLO"))
        s = self.parse("Astrahus [FRT] [AB - Some Citadel]")
        self.assertEqual((s.kind, s.name), (ActorKind.STRUCTURE, "AB - Some Citadel"))
        m = self.parse("Mobile Large Warp Disruptor I [Mobile Large Warp Disruptor I]")
        self.assertEqual(m.kind, ActorKind.STRUCTURE)

    def test_npcs_unknowns_and_empty(self):
        self.assertEqual(self.parse("Guristas Murderer").kind, ActorKind.NPC)
        self.assertEqual(self.parse("Blood Raiders Cruor").kind, ActorKind.UNKNOWN)
        self.assertEqual(self.parse("").kind, ActorKind.EMPTY)
        self.assertEqual(self.parse("-").kind, ActorKind.EMPTY)

    def test_shape(self):
        self.assertEqual(tokenize_overview("Loki <A>[B] C", CATALOG).shape, "S<[b")


class SplitTest(TestCase):
    def test_split_last_dash(self):
        self.assertEqual(split_last_dash("Loki - Pithum Booster"), ("Loki", "Pithum Booster"))
        self.assertEqual(split_last_dash("Augoror [PS] [P] - - Module"), ("Augoror [PS] [P]", "Module"))
        self.assertEqual(split_last_dash("Astrahus [FRT] [AB - Some] - - ECM"), ("Astrahus [FRT] [AB - Some]", "ECM"))
        self.assertEqual(split_last_dash("- Module"), ("", "Module"))
        self.assertEqual(split_last_dash("NoModule"), ("NoModule", None))

    def test_split_damage_body(self):
        self.assertEqual(
            split_damage_body("A[B](Loki) - 425mm AutoCannon II", CATALOG.is_weapon_name),
            ("A[B](Loki)", "425mm AutoCannon II"),
        )
        self.assertEqual(split_damage_body("Centii Plague", CATALOG.is_weapon_name), ("Centii Plague", None))
        self.assertEqual(
            split_damage_body("Mobile Small Warp Disruptor II - 720mm Howitzer Artillery II", CATALOG.is_weapon_name),
            ("Mobile Small Warp Disruptor II", "720mm Howitzer Artillery II"),
        )
        self.assertEqual(split_damage_body("Odd - Thing", CATALOG.is_weapon_name), ("Odd", "Thing"))

    def test_split_tackle(self):
        self.assertEqual(split_tackle("you to Drake"), ("you", "Drake"))
        self.assertEqual(split_tackle("Nighthawk to you!"), ("Nighthawk", "you"))
        self.assertEqual(split_tackle("Crow to Cenotaph"), ("Crow", "Cenotaph"))
        self.assertEqual(
            split_tackle("Arazu [Pilot Golf] - to Exequror [FRT] [Fly to Win] -"),
            ("Arazu [Pilot Golf]", "Exequror [FRT] [Fly to Win]"),
        )
        self.assertEqual(split_tackle("Lonely"), ("Lonely", ""))


class LinesTest(TestCase):
    def match(self, text, color=None):
        return match_combat(text, color, CATALOG.is_weapon_name)

    def test_damage_with_repeated_segment_and_deflected(self):
        ev = self.match(
            "211 to DO6H-Q - Some Hive[B.CI](Keepstar) - 250mm Railgun II - Hits - 250mm Railgun II - Hits (211 deflected)"
        )
        self.assertEqual((ev.kind, ev.amount, ev.quality, ev.deflected), (Kind.DAMAGE, 211, Quality.HITS, 211))
        self.assertEqual(ev.tgt, "DO6H-Q - Some Hive[B.CI](Keepstar)")

    def test_reps_cap_nos_jams(self):
        self.assertEqual(self.match("-0 remote capacitor transmitted to Basilisk - Large Cap").kind, Kind.CAP)
        self.assertEqual(self.match("24 remote armor repaired to Damaged Vessel - Rep").kind, Kind.REP_ARMOR)
        self.assertEqual(self.match("5 remote hull repaired by Oneiros - Hull Rep").kind, Kind.REP_HULL)
        out = self.match("+8 GJ energy drained from Pilgrim - Small Nos")
        self.assertEqual((out.kind, out.src), (Kind.NOS, "you"))
        inc = self.match("-8 GJ energy drained to Curse - Heavy Nos")
        self.assertEqual(inc.tgt, "you")
        jam_in = self.match("You're jammed by Kitsune - Gravimetric ECM II")
        self.assertEqual((jam_in.kind, jam_in.src), (Kind.JAM, "Kitsune"))
        jam_out = self.match("Draugur [VAULT] [S-4U] [Pilotmike66] - jammed - Gloom Radar ECM")
        self.assertEqual(jam_out.tgt, "Draugur [VAULT] [S-4U] [Pilotmike66]")
        burst = self.match("Your target locks broken by Tempest - Burst Jammer II")
        self.assertTrue(burst.aoe_hint)
        self.assertTrue(self.match("Tempest target locks broken - Burst Jammer II").aoe_hint)

    def test_neut_direction_comes_from_colour(self):
        text = "180 GJ energy neutralized Vexor Navy Issue - Medium Energy Neutralizer II"
        self.assertEqual(self.match(text, "0xff7fffff").src, "you")
        self.assertEqual(self.match(text, "0xffe57f7f").tgt, "you")
        self.assertIsNone(self.match(text, "0xff000000"))

    def test_misses(self):
        self.assertEqual(
            self.match("Your group of 425mm AutoCannon II misses Pilot November completely - 425mm AutoCannon II").tgt,
            "Pilot November",
        )
        self.assertEqual(self.match("Your Hobgoblin I misses Centii Plague completely - Hobgoblin I").kind, Kind.MISS)
        drone = self.match("Valkyrie II belonging to Pilot Oscar misses you completely - Valkyrie II")
        self.assertEqual((drone.src, drone.owner), ("Valkyrie II", "Pilot Oscar"))
        self.assertIsNone(self.match("Centii Plague misses you completely").module)

    def test_unknown(self):
        self.assertIsNone(self.match("Something entirely different"))
        self.assertTrue(skeleton("12 strange Line [x] here"))


class ReaderTest(TestCase):
    def test_header_filename_and_decoding(self):
        self.assertEqual(read_header(HEADER), ("Pilot Listener", 1792957905))
        self.assertEqual(parse_filename("20261025_195145_2113328928.txt"), (1792957905, 2113328928))
        self.assertEqual(parse_filename("20261025_195145.txt"), (1792957905, None))
        self.assertEqual(parse_filename("fight.txt"), (None, None))
        parsed = parse_log(HEADER.encode("cp1252") + "[ 2026.10.25 20:19:30 ] (combat) é\n".encode("cp1252"))
        self.assertEqual(parsed.combat_lines, 1)

    def test_full_pipeline_on_synthetic_lines(self):
        data = log_text(
            DMG_OUT.format(n=351, label="Pilot Kilo I[IMBCN](Cenotaph)", weapon="425mm AutoCannon II", q="Grazes"),
            DMG_IN.format(
                n=29, label="Teamsesh[FFEW](Sleipnir)", weapon="Domination Medium Proton Smartbomb", q="Hits"
            ),
            DMG_IN.format(n=376, label="Orthrus", weapon="", q="Hits").replace(" -  - ", " - "),
            DMG_OUT.format(n=500, label="Orthrus", weapon="800mm Repeating Cannon II", q="Hits"),
            REP_IN.format(n=250, label="Loki", module="Pithum B-Type Medium Remote Shield Booster"),
            NEUT.format(color="0xff7fffff", n=180, label="Vexor Navy Issue", module="Medium Energy Neutralizer II"),
            TACKLE.format(src="Crow", tgt="<u><b>Cenotaph</b></u>"),
            TACKLE.format(src="Nighthawk", tgt="you!"),
            "garbage line we do not know",
        )
        parsed = parse_log(data, CATALOG)
        self.assertEqual(parsed.listener, "Pilot Listener")
        self.assertEqual(parsed.combat_lines, 9)
        self.assertEqual(parsed.matched_lines, 8)
        self.assertTrue(parsed.warnings)
        by_line = {e.line_no: e for e in parsed.events}
        first = parsed.events[0]
        self.assertEqual(
            (first.kind, first.src.kind, first.tgt.name, first.amount),
            (Kind.DAMAGE, ActorKind.LISTENER, "Pilot Kilo I", 351),
        )
        self.assertTrue(first.flags & Flag.CAN_MISS)
        smart = [e for e in parsed.events if e.module == "Domination Medium Proton Smartbomb"][0]
        self.assertTrue(smart.flags & Flag.AOE)
        orthrus = [e for e in parsed.events if e.tgt.name == "Orthrus" or e.src.name == "Orthrus"]
        self.assertTrue(all((e.tgt if e.tgt.name == "Orthrus" else e.src).kind == ActorKind.NPC for e in orthrus))
        rep = [e for e in parsed.events if e.kind == Kind.REP_SHIELD][0]
        self.assertEqual((rep.src.kind, rep.src.ship), (ActorKind.SHIP_ONLY, "Loki"))
        observed = [e for e in parsed.events if e.flags & Flag.OBSERVED]
        self.assertEqual(len(observed), 1)
        self.assertTrue(by_line)

    def test_bare_pilot_with_player_module_is_a_pilot(self):
        data = log_text(
            DMG_IN.format(n=263, label="Pilot Papa", weapon="Dark Blood Large EMP Smartbomb", q="Hits"),
            "Pilot Quebec misses you completely - 75mm Gatling Rail II",
        )
        parsed = parse_log(data, CATALOG)
        self.assertEqual(parsed.events[0].src.kind, ActorKind.PILOT)
        self.assertEqual(parsed.events[1].src.kind, ActorKind.PILOT)

    def test_context_lines(self):
        data = log_text(
            ("None", "Jumping from Jita to Perimeter"),
            ("None", "Undocking from Nema X - Moon 6 - Hedion University to Nema solar system."),
            (
                "question",
                '<a href="showinfo:1379//2120273592">PaidProgramming</a> wants you to join their fleet, do you accept?',
            ),
            (
                "question",
                'Are you sure you want to join the fleet commanded by <a href="showinfo:1376//90000099">Pilot Romeo</a>?',
            ),
            ("notify", "Following Pilot Sierra in warp"),
            ("notify", "Regrouping to Pilot Sierra"),
            ("notify", "Teamsesh has been invited to your fleet as a squad member"),
            ("notify", "Disembarking from ship"),
            ("notify", "Starting clone jumping"),
            ("notify", "425mm AutoCannon II deactivates as Pilot Tängo begins to explode."),
            ("notify", "Local spatial phenomena may cause strange effects on your ship systems."),
            ("notify", "Your cloak deactivates due to proximity to a nearby Wormhole K162."),
            ("notify", "Ship stopping"),
            ("info", "Something else"),
        )
        kinds = [(c.kind, c.name or c.system, c.character_id) for c in parse_log(data, CATALOG).context]
        self.assertEqual(
            kinds,
            [
                (ContextKind.SYSTEM, "Perimeter", None),
                (ContextKind.SYSTEM, "Nema", None),
                (ContextKind.FLEET_INVITE, "PaidProgramming", 2120273592),
                (ContextKind.FLEET_JOIN, "Pilot Romeo", 90000099),
                (ContextKind.FLEET_WARP, "Pilot Sierra", None),
                (ContextKind.REGROUP, "Pilot Sierra", None),
                (ContextKind.FLEET_MEMBER, "Teamsesh", None),
                (ContextKind.RESHIP, None, None),
                (ContextKind.CLONE_JUMP, None, None),
                (ContextKind.KILL_NOTICE, "Pilot Tängo", None),
                (ContextKind.WSPACE_HINT, None, None),
                (ContextKind.WORMHOLE_HINT, None, None),
            ],
        )
        from fightlog.parser import ContextEvent

        c = ContextEvent(1, ContextKind.SYSTEM, system="Jita", line_no=3)
        self.assertEqual(ContextEvent.from_dict(c.to_dict()), c)

    def test_non_english_detection(self):
        lines = ["Schaden {} an X".format(i) for i in range(25)]
        self.assertFalse(parse_log(log_text(*lines), CATALOG).is_english)
        self.assertTrue(parse_log(log_text("short"), CATALOG).is_english)


class ShapesFixtureTest(TestCase):
    """One anonymized line for each of the 2,000+ combat line shapes in the corpus."""

    def test_every_shape_parses(self):
        data = gzip.decompress((FIXTURES / "lines" / "shapes.txt.gz").read_bytes())
        parsed = parse_log(data, CATALOG)
        self.assertGreater(parsed.combat_lines, 2000)
        self.assertEqual(parsed.matched_lines, parsed.combat_lines, dict(parsed.warnings))

    def test_fixtures_hold_no_real_names(self):
        """tools/anonymize.py replaces every player name, all-digit ones included."""
        for path in sorted(FIXTURES.rglob("*.txt.gz")):
            parsed = parse_log(gzip.decompress(path.read_bytes()), CATALOG)
            for event in parsed.events:
                for actor in (event.src, event.tgt):
                    if actor.kind in (ActorKind.PILOT, ActorKind.UNKNOWN):
                        self.assertFalse(actor.name and actor.name.isdigit(), f"{path.name}: {actor.name}")
                    self.assertFalse(actor.owner and actor.owner.isdigit(), f"{path.name}: {actor.owner}")


class CatalogTest(TestCase):
    def test_lookups(self):
        self.assertGreater(CATALOG.type_count, 10000)
        self.assertTrue(CATALOG.is_player_hull("Loki"))
        self.assertTrue(CATALOG.is_npc_name("Emergent Warden"))
        self.assertTrue(CATALOG.is_aoe("Domination Medium Proton Smartbomb"))
        self.assertTrue(CATALOG.is_aoe("Burst Jammer II"))
        self.assertFalse(CATALOG.is_aoe("425mm AutoCannon II"))
        self.assertTrue(CATALOG.can_miss("425mm AutoCannon II"))
        self.assertTrue(CATALOG.can_miss("Warrior II"))
        self.assertFalse(CATALOG.can_miss("Scourge Rage Heavy Assault Missile"))
        self.assertTrue(CATALOG.is_structure("Ansiblex Jump Gate"))
        self.assertTrue(CATALOG.is_charge_group("Light Missile"))
        self.assertEqual(CATALOG.longest_type_prefix("Brutix Pilotecho98")[1], "Pilotecho98")
        self.assertEqual(CATALOG.longest_type_prefix("Nothing Here"), (None, "Nothing Here"))
        self.assertEqual(CATALOG.get(""), [])
        self.assertEqual(CATALOG.by_id(29990).name, "Loki")
        self.assertIsNone(CATALOG.by_id(None))
        system = CATALOG.system(31001176)
        self.assertTrue(system.is_wspace)
        self.assertEqual(CATALOG.system_by_name("jita").system_id, 30000142)
        self.assertIsNone(CATALOG.system_by_name(None))
