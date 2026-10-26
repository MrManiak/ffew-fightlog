"""Actor labels in combat lines.

Damage and miss lines always use ``Pilot[CORP](Ship)`` whatever the client's
settings. Every other line uses the listener's *overview ship label*, whose
fields, order and brackets are configurable per EVE account. Known layouts:

    Loki                                         ship type only
    Ikitursa [Pilot Alfa]                        ship [pilot]
    Augoror [PS] [SLUP] [Pilot Bravo Two]        ship [alliance] [corp] [pilot]
    Loki <SPORK>[SLAPD] PilotCharlie             stock: ship <alliance>[corp] pilot
    Brutix Pilotecho98 (HIREV) [-ENIE] - “x”     ship pilot (corp) [alliance] - “ship name”
    Astrahus [FRT] [AB - Some Citadel]           structure: the name sits in the pilot slot
    Acolyte II [BCAUS] [KS88] [Acolyte II] -     drone: its own name in the pilot slot

``LabelTemplate`` learns, per log, which bracket holds the corp ticker when a
layout is ambiguous, using pilots whose damage lines identify them exactly.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from fightlog.catalog import CAT_CHARGE, CAT_DRONE, CAT_ENTITY, CAT_FIGHTER, CAT_SHIP, STRUCTURE_CATEGORIES, Catalog

from .events import Actor, ActorKind

DAMAGE_LABEL_RE = re.compile(r"^(?P<name>.*)\[(?P<corp>[^\[\]]*)\]\((?P<ship>[^()]*)\)$")
SHIP_NAME_RE = re.compile(r"\s+-\s+[“\"](?P<name>.*)[”\"]\s*$")
BRACKET_RE = re.compile(r"<[^<>]*>|\[[^\[\]]*\]|\([^()]*\)")
MAX_TICKER = 5


def parse_damage_label(text: str, catalog: Catalog) -> Actor:
    """``Pilot[CORP](Ship)``; anything else is a bare name, classified later."""
    text = text.strip()
    match = DAMAGE_LABEL_RE.match(text)
    if not match:
        return Actor(ActorKind.UNKNOWN, name=text or None, raw=text)
    name = match.group("name").strip()
    corp = match.group("corp") or None
    ship = match.group("ship").strip()
    return Actor(_kind_for_type(ship, name, catalog), name=name or None, ship=ship, corp=corp, raw=text)


def _kind_for_type(ship: str, name: str | None, catalog: Catalog, *, labelled: bool = True) -> str:
    categories = catalog.categories(ship)
    if CAT_SHIP in categories:
        return ActorKind.PILOT
    if categories & {CAT_DRONE, CAT_FIGHTER}:
        return ActorKind.DRONE
    if CAT_CHARGE in categories:
        return ActorKind.CHARGE
    if categories & STRUCTURE_CATEGORIES:
        return ActorKind.STRUCTURE
    if CAT_ENTITY in categories and not labelled:
        return ActorKind.NPC
    # Not in the catalog (a new or renamed type). A name equal to its type is a
    # drone-like object; anything else labelled is most likely a pilot in a new hull.
    if name and name == ship:
        return ActorKind.DRONE
    return ActorKind.PILOT


@dataclass
class LabelTokens:
    raw: str
    ship: str | None = None
    first_rest: str | None = None  # text after the ship type in the first bare segment
    brackets: list[tuple[str, str]] = field(default_factory=list)  # (opening char, content)
    trailing: str | None = None  # bare text after the brackets (stock layout pilot)
    ship_name: str | None = None

    @property
    def shape(self) -> str:
        parts = ["S" if self.ship else "?"]
        if self.first_rest:
            parts.append("b")
        parts.extend(o for o, _ in self.brackets)
        if self.trailing:
            parts.append("b")
        if self.ship_name:
            parts.append("q")
        return "".join(parts)


def tokenize_overview(text: str, catalog: Catalog) -> LabelTokens:
    text = text.strip()
    tokens = LabelTokens(raw=text)
    match = SHIP_NAME_RE.search(text)
    if match:
        tokens.ship_name = match.group("name")
        text = text[: match.start()]
    text = re.sub(r"(?:\s+-)+\s*$", "", text).strip()
    if text == "-":
        text = ""
    pieces: list[tuple[str, str]] = []
    pos = 0
    for m in BRACKET_RE.finditer(text):
        bare = text[pos : m.start()].strip()
        if bare:
            pieces.append(("bare", bare))
        token = m.group(0)
        pieces.append((token[0], token[1:-1]))
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        pieces.append(("bare", tail))
    if not pieces:
        return tokens
    if pieces[0][0] == "bare":
        ship_info, rest = catalog.longest_type_prefix(pieces[0][1])
        if ship_info:
            tokens.ship = ship_info.name
            tokens.first_rest = rest or None
        else:
            tokens.first_rest = pieces[0][1]
        pieces = pieces[1:]
    trailing = []
    for kind, value in pieces:
        if kind == "bare":
            trailing.append(value)
        else:
            tokens.brackets.append((kind, value))
    tokens.trailing = " ".join(t for t in trailing if t and t != "-") or None
    return tokens


@dataclass
class LabelTemplate:
    """What a log's overview label layout means, learned from known pilots."""

    one_ticker: str = "corp"  # role of a single [x] before a bracketed pilot
    two_tickers: tuple[str, str] = ("alliance", "corp")
    votes: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {"one_ticker": self.one_ticker, "two_tickers": list(self.two_tickers), "votes": dict(self.votes)}

    def learn(self, tokens: LabelTokens, known_corps: dict[str, str]) -> None:
        """Vote using a label whose pilot we already know from damage lines."""
        squares = [v for o, v in tokens.brackets if o == "["]
        if len(squares) < 2:
            return
        pilot, tickers = squares[-1], squares[:-1]
        corp = known_corps.get(pilot)
        if not corp:
            return
        if len(tickers) == 1:
            self.votes["one:" + ("corp" if tickers[0] == corp else "alliance")] += 1
        elif len(tickers) == 2:
            if tickers[1] == corp:
                self.votes["two:alliance,corp"] += 1
            elif tickers[0] == corp:
                self.votes["two:corp,alliance"] += 1

    def settle(self) -> None:
        if self.votes["one:alliance"] > self.votes["one:corp"]:
            self.one_ticker = "alliance"
        if self.votes["two:corp,alliance"] > self.votes["two:alliance,corp"]:
            self.two_tickers = ("corp", "alliance")


def parse_overview_label(tokens: LabelTokens, catalog: Catalog, template: LabelTemplate | None = None) -> Actor:
    template = template or LabelTemplate()
    raw = tokens.raw
    if not raw or raw == "-":
        return Actor(ActorKind.EMPTY, raw=raw)
    corp = alliance = pilot = None
    angles = [v for o, v in tokens.brackets if o == "<"]
    rounds = [v for o, v in tokens.brackets if o == "(" and len(v) <= MAX_TICKER]
    squares = [v for o, v in tokens.brackets if o == "["]
    if angles:
        alliance = angles[0]
    if rounds:
        corp = rounds[0]
    bare_pilot = tokens.first_rest if tokens.ship else None
    bare_pilot = bare_pilot or tokens.trailing
    if bare_pilot:
        pilot = bare_pilot
        for value in squares:
            if corp is None:
                corp = value
            elif alliance is None:
                alliance = value
    elif squares:
        pilot = squares[-1]
        tickers = squares[:-1]
        if len(tickers) == 1:
            if template.one_ticker == "corp" and corp is None:
                corp = tickers[0]
            elif alliance is None:
                alliance = tickers[0]
        elif len(tickers) >= 2:
            first_role, second_role = template.two_tickers
            values = {first_role: tickers[-2], second_role: tickers[-1]}
            corp = corp or values["corp"]
            alliance = alliance or values["alliance"]
    ship = tokens.ship
    if ship is None:
        # No ship type at the front: an NPC, or a name we don't know.
        name = tokens.first_rest or raw
        if catalog.is_npc_name(name):
            return Actor(ActorKind.NPC, name=name, raw=raw)
        if catalog.is_player_hull(name):
            return Actor(ActorKind.SHIP_ONLY, ship=name, raw=raw)
        return Actor(ActorKind.UNKNOWN, name=name, raw=raw)
    categories = catalog.categories(ship)
    if pilot:
        if pilot == ship or catalog.has_category(pilot, CAT_DRONE, CAT_FIGHTER):
            kind = ActorKind.DRONE if categories & {CAT_DRONE, CAT_FIGHTER} else ActorKind.STRUCTURE
            return Actor(kind, name=pilot, ship=ship, corp=corp, alliance=alliance, raw=raw)
        if categories & STRUCTURE_CATEGORIES:
            return Actor(ActorKind.STRUCTURE, name=pilot, ship=ship, corp=corp, alliance=alliance, raw=raw)
        if categories & {CAT_DRONE, CAT_FIGHTER}:
            return Actor(ActorKind.DRONE, name=ship, ship=ship, corp=corp, alliance=alliance, owner=pilot, raw=raw)
        return Actor(ActorKind.PILOT, name=pilot, ship=ship, corp=corp, alliance=alliance, raw=raw)
    if CAT_SHIP in categories and catalog.is_player_hull(ship):
        return Actor(ActorKind.SHIP_ONLY, ship=ship, corp=corp, alliance=alliance, raw=raw)
    if categories & {CAT_DRONE, CAT_FIGHTER}:
        return Actor(ActorKind.DRONE, name=ship, ship=ship, corp=corp, alliance=alliance, raw=raw)
    if categories & STRUCTURE_CATEGORIES:
        return Actor(ActorKind.STRUCTURE, name=ship, ship=ship, corp=corp, alliance=alliance, raw=raw)
    if CAT_ENTITY in categories:
        return Actor(ActorKind.NPC, name=raw, raw=raw)
    return Actor(ActorKind.UNKNOWN, name=raw, ship=ship, raw=raw)
