"""Read one gamelog file into a ParsedLog."""

import calendar
import re

from fightlog.catalog import Catalog, load_catalog

from .classify import BareNameClassifier
from .context import match_context
from .events import Actor, ActorKind, Event, Flag, Kind, ParsedLog
from .labels import LabelTemplate, parse_damage_label, parse_overview_label, tokenize_overview
from .markup import clean, leading_color
from .patterns import DAMAGE_STYLE, DRONE_STYLE, OVERVIEW_STYLE, YOU, RawEvent, match_combat, skeleton

LINE_RE = re.compile(r"^\[ (\d{4})\.(\d{2})\.(\d{2}) (\d{2}):(\d{2}):(\d{2}) \] \((\w+)\) (.*)$")
LISTENER_RE = re.compile(r"^\s*Listener:\s*(?P<name>.+?)\s*$")
SESSION_RE = re.compile(r"^\s*Session Started:\s*(\d{4})\.(\d{2})\.(\d{2}) (\d{2}):(\d{2}):(\d{2})")
FILENAME_RE = re.compile(r"(?P<date>\d{8})_(?P<time>\d{6})(?:_(?P<character_id>\d+))?\.txt$")


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def epoch(parts) -> int:
    y, mo, d, h, mi, s = (int(p) for p in parts)
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def read_header(text: str) -> tuple[str | None, int | None]:
    listener = session = None
    for line in text.splitlines()[:12]:
        m = LISTENER_RE.match(line)
        if m:
            listener = m.group("name")
        m = SESSION_RE.match(line)
        if m:
            session = epoch(m.groups())
    return listener, session


def parse_filename(filename: str) -> tuple[int | None, int | None]:
    """'20261025_195145_2113328928.txt' → (session epoch, character ID)."""
    m = FILENAME_RE.search(filename or "")
    if not m:
        return None, None
    d, t = m.group("date"), m.group("time")
    session = epoch((d[0:4], d[4:6], d[6:8], t[0:2], t[2:4], t[4:6]))
    cid = m.group("character_id")
    return session, int(cid) if cid else None


def parse_log(data: bytes, catalog: Catalog | None = None) -> ParsedLog:
    catalog = catalog or load_catalog()
    text = decode(data)
    listener_name, session = read_header(text)
    parsed = ParsedLog(listener=listener_name, session_started=session)
    raw_events: list[tuple[int, int, RawEvent]] = []
    # Split on "\n" only: splitlines() also breaks on U+2028 and friends, which some
    # messages contain, and line numbers must match what an editor shows.
    for line_no, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        parsed.line_count = line_no
        m = LINE_RE.match(line)
        if not m:
            continue
        t = epoch(m.groups()[:6])
        kind, msg = m.group(7), m.group(8)
        if kind == "combat":
            parsed.combat_lines += 1
            cleaned = clean(msg)
            event = match_combat(cleaned, leading_color(msg), catalog.is_weapon_name)
            if event is None:
                parsed.warnings[skeleton(cleaned)] += 1
                continue
            parsed.matched_lines += 1
            raw_events.append((t, line_no, event))
        else:
            ctx = match_context(t, kind, msg, line_no)
            if ctx:
                parsed.context.append(ctx)

    listener = Actor(ActorKind.LISTENER, name=listener_name, raw=YOU)

    # Pass 2: damage labels name pilots exactly.
    damage_cache: dict[str, Actor] = {}
    known_pilots: dict[str, Actor] = {}
    for _, _, ev in raw_events:
        for label, style in ((ev.src, ev.src_style), (ev.tgt, ev.tgt_style)):
            if style == DAMAGE_STYLE and label != YOU and label not in damage_cache:
                actor = parse_damage_label(label, catalog)
                damage_cache[label] = actor
                if actor.kind == ActorKind.PILOT and actor.name and actor.corp:
                    known_pilots[actor.name] = actor
    known_corps = {name: actor.corp for name, actor in known_pilots.items()}

    # Pass 3: learn the overview label layout from pilots we already know.
    template = LabelTemplate()
    token_cache = {}
    for _, _, ev in raw_events:
        for label, style in ((ev.src, ev.src_style), (ev.tgt, ev.tgt_style)):
            if style == OVERVIEW_STYLE and label != YOU and label not in token_cache:
                tokens = tokenize_overview(label, catalog)
                token_cache[label] = tokens
                template.learn(tokens, known_corps)
                parsed.label_shapes[tokens.shape] += 1
    template.settle()
    parsed.label_template = template.to_dict()

    # Pass 4: evidence for bare names.
    classifier = BareNameClassifier(catalog, known_pilots)
    for _, _, ev in raw_events:
        if ev.tgt == YOU and ev.src_style == DAMAGE_STYLE and ev.kind in (Kind.DAMAGE, Kind.MISS):
            actor = damage_cache.get(ev.src)
            if actor and actor.kind == ActorKind.UNKNOWN:
                classifier.note_incoming(ev.src, ev.module)

    overview_cache: dict[str, Actor] = {}

    def resolve(label: str, style: str, owner: str | None) -> Actor:
        if label == YOU:
            return listener
        if style == DRONE_STYLE:
            return Actor(ActorKind.DRONE, name=label, ship=label, owner=owner, raw=f"{label} belonging to {owner}")
        if style == DAMAGE_STYLE:
            actor = damage_cache[label] if label in damage_cache else parse_damage_label(label, catalog)
            return classifier.classify(label) if actor.kind == ActorKind.UNKNOWN else actor
        actor = overview_cache.get(label)
        if actor is None:
            actor = parse_overview_label(token_cache[label], catalog, template)
            actor = classifier.demote_ship_only(actor)
            if actor.kind == ActorKind.UNKNOWN and actor.name in known_pilots:
                actor = known_pilots[actor.name]
            overview_cache[label] = actor
        return actor

    for t, line_no, ev in raw_events:
        src = resolve(ev.src, ev.src_style, ev.owner)
        tgt = resolve(ev.tgt, ev.tgt_style, None)
        flags = Flag.NONE
        module = ev.module
        if ev.aoe_hint or (module and catalog.is_aoe(module)):
            flags |= Flag.AOE
        if ev.src_style == DRONE_STYLE or (module and catalog.is_drone_weapon(module)):
            flags |= Flag.DRONE
        if ev.kind == Kind.MISS or (ev.kind == Kind.DAMAGE and module and catalog.can_miss(module)):
            flags |= Flag.CAN_MISS
        if ev.observed:
            flags |= Flag.OBSERVED
        if tgt.kind in ActorKind.NON_SHIP:
            flags |= Flag.NON_SHIP_TARGET
        parsed.events.append(Event(t, ev.kind, src, tgt, module, ev.amount, ev.quality, flags, line_no))
    parsed.events.sort(key=lambda e: (e.t, e.line_no))
    return parsed
