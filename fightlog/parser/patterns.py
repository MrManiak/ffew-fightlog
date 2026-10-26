"""Combat line formats → raw events (labels are still strings here).

Every format in the corpus inventory (docs/log-formats.md) has a matcher. Lines
that match nothing are counted as warnings by shape, never raised.
"""

import re
from dataclasses import dataclass

from .events import QUALITY_WORDS, Kind, Quality

NEUT_OUT_COLOR = "0xff7fffff"
NEUT_IN_COLOR = "0xffe57f7f"

QUALITY = "|".join(sorted(QUALITY_WORDS, key=len, reverse=True))
DAMAGE_RE = re.compile(
    rf"^(?P<amount>\d+) (?P<dir>to|from) (?P<body>.+?) - (?P<q>{QUALITY})"
    rf"(?: - (?:.+? - )?(?:{QUALITY}))?(?: \((?P<deflected>\d+) deflected\))?$"
)
LABELLED_BODY_RE = re.compile(r"^(?P<label>.*\[[^\[\]]*\]\([^()]*\))(?: - (?P<weapon>.+))?$")
REP_RE = re.compile(
    r"^[+-]?(?P<amount>\d+) remote (?P<what>shield|armor|hull) (?:boosted|repaired) (?P<dir>by|to)(?: (?P<rest>.*))?$"
)
CAP_RE = re.compile(r"^[+-]?(?P<amount>\d+) remote capacitor transmitted (?P<dir>by|to)(?: (?P<rest>.*))?$")
NEUT_RE = re.compile(r"^(?P<amount>\d+) GJ energy neutralized (?P<rest>.+)$")
NOS_RE = re.compile(r"^(?P<sign>[+-])(?P<amount>\d+) GJ energy drained (?P<dir>from|to)(?: (?P<rest>.*))?$")
JAM_IN_RE = re.compile(r"^You're jammed by (?P<rest>.+)$")
JAM_OUT_RE = re.compile(r"^(?P<label>.+?) jammed - (?P<module>.+)$")
BURST_IN_RE = re.compile(r"^Your target locks broken by (?P<rest>.+)$")
BURST_OUT_RE = re.compile(r"^(?P<label>.+?) target locks broken - (?P<module>.+)$")
TACKLE_RE = re.compile(r"^Warp (?P<what>scramble|disruption) attempt from (?P<body>.+)$")
MISS_GROUP_OUT_RE = re.compile(r"^Your group of (?P<weapon>.+?) misses (?P<tgt>.+) completely - (?P<module>.+)$")
MISS_OUT_RE = re.compile(r"^Your (?P<weapon>.+?) misses (?P<tgt>.+) completely - (?P<module>.+)$")
MISS_DRONE_IN_RE = re.compile(r"^(?P<drone>.+?) belonging to (?P<owner>.+) misses you completely - (?P<module>.+)$")
MISS_IN_RE = re.compile(r"^(?P<src>.+) misses you completely(?: - (?P<module>.+))?$")

REP_KIND = {"shield": Kind.REP_SHIELD, "armor": Kind.REP_ARMOR, "hull": Kind.REP_HULL}

# Label styles
DAMAGE_STYLE = "damage"  # Pilot[CORP](Ship) or a bare name
OVERVIEW_STYLE = "overview"  # the listener's overview ship label
DRONE_STYLE = "drone"  # a drone named with its owner ("X belonging to Y")
YOU = "you"


@dataclass
class RawEvent:
    kind: Kind
    src: str  # label text, or "you"
    tgt: str
    src_style: str
    tgt_style: str
    module: str | None
    amount: int = 0
    quality: Quality = Quality.NONE
    aoe_hint: bool = False  # burst jammers
    observed: bool = False
    deflected: int = 0
    owner: str | None = None  # "Warrior II belonging to <owner> misses you"


def split_last_dash(rest: str | None) -> tuple[str, str | None]:
    """'Label - Module' → (label, module). Labels may contain ' - ' and end in ' -'."""
    rest = (rest or "").strip()
    if rest.startswith("- "):
        return "", rest[2:].strip() or None
    idx = _last_top_level(rest, " - ")
    if idx < 0:
        return rest, None
    label = rest[:idx].rstrip()
    module = rest[idx + 3 :].strip() or None
    while label.endswith(" -"):
        label = label[:-2].rstrip()
    if label == "-":
        label = ""
    return label, module


def _depth_positions(text: str):
    depth = 0
    for i, ch in enumerate(text):
        if ch in "[(<“":
            depth += 1
        elif ch in "])>”" and depth:
            depth -= 1
        yield i, depth


def _last_top_level(text: str, sep: str) -> int:
    best = -1
    depths = dict(_depth_positions(text))
    start = 0
    while True:
        idx = text.find(sep, start)
        if idx < 0:
            return best
        if depths.get(idx, 0) == 0:
            best = idx
        start = idx + 1


def _top_level_positions(text: str, sep: str) -> list[int]:
    depths = dict(_depth_positions(text))
    out, start = [], 0
    while True:
        idx = text.find(sep, start)
        if idx < 0:
            return out
        if depths.get(idx, 0) == 0:
            out.append(idx)
        start = idx + 1


def split_damage_body(body: str, is_weapon) -> tuple[str, str | None]:
    """Damage body → (label, weapon). Labelled bodies end the label at the last ](...)."""
    match = LABELLED_BODY_RE.match(body)
    if match:
        return match.group("label"), match.group("weapon")
    cuts = _top_level_positions(body, " - ")
    for idx in reversed(cuts):
        if is_weapon(body[idx + 3 :]):
            return body[:idx], body[idx + 3 :]
    if cuts:
        return body[: cuts[-1]], body[cuts[-1] + 3 :]
    return body, None


def split_tackle(body: str) -> tuple[str, str]:
    if body.startswith("you to "):
        return YOU, _strip_label(body[len("you to ") :])
    if body.endswith(" to you!"):
        return _strip_label(body[: -len(" to you!")]), YOU
    cuts = _top_level_positions(body, " to ")
    if not cuts:
        return _strip_label(body), ""
    preferred = [i for i in cuts if body[:i].endswith(" -")]
    idx = preferred[0] if preferred else cuts[0]
    return _strip_label(body[:idx]), _strip_label(body[idx + 4 :])


def _strip_label(text: str) -> str:
    text = text.strip().rstrip("!").strip()
    while text.endswith(" -"):
        text = text[:-2].rstrip()
    return "" if text == "-" else text


def match_combat(text: str, color: str | None, is_weapon) -> RawEvent | None:
    """Turn one cleaned combat line into a RawEvent, or None if no format matches."""
    m = DAMAGE_RE.match(text)
    if m:
        label, weapon = split_damage_body(m.group("body"), is_weapon)
        outgoing = m.group("dir") == "to"
        return RawEvent(
            kind=Kind.DAMAGE,
            src=YOU if outgoing else label,
            tgt=label if outgoing else YOU,
            src_style=DAMAGE_STYLE,
            tgt_style=DAMAGE_STYLE,
            module=weapon,
            amount=int(m.group("amount")),
            quality=QUALITY_WORDS[m.group("q")],
            deflected=int(m.group("deflected") or 0),
        )
    for regex, kind_of in ((REP_RE, lambda g: REP_KIND[g["what"]]), (CAP_RE, lambda g: Kind.CAP)):
        m = regex.match(text)
        if m:
            label, module = split_last_dash(m.group("rest"))
            incoming = m.group("dir") == "by"
            return RawEvent(
                kind=kind_of(m.groupdict()),
                src=label if incoming else YOU,
                tgt=YOU if incoming else label,
                src_style=OVERVIEW_STYLE,
                tgt_style=OVERVIEW_STYLE,
                module=module,
                amount=int(m.group("amount")),
            )
    m = NEUT_RE.match(text)
    if m:
        if color not in (NEUT_OUT_COLOR, NEUT_IN_COLOR):
            return None
        label, module = split_last_dash(m.group("rest"))
        outgoing = color == NEUT_OUT_COLOR
        return RawEvent(
            kind=Kind.NEUT,
            src=YOU if outgoing else label,
            tgt=label if outgoing else YOU,
            src_style=OVERVIEW_STYLE,
            tgt_style=OVERVIEW_STYLE,
            module=module,
            amount=int(m.group("amount")),
        )
    m = NOS_RE.match(text)
    if m:
        label, module = split_last_dash(m.group("rest"))
        outgoing = m.group("dir") == "from"  # "+N drained from X": we drained X
        return RawEvent(
            kind=Kind.NOS,
            src=YOU if outgoing else label,
            tgt=label if outgoing else YOU,
            src_style=OVERVIEW_STYLE,
            tgt_style=OVERVIEW_STYLE,
            module=module,
            amount=int(m.group("amount")),
        )
    m = JAM_IN_RE.match(text) or BURST_IN_RE.match(text)
    if m:
        label, module = split_last_dash(m.group("rest"))
        return RawEvent(
            kind=Kind.JAM,
            src=label,
            tgt=YOU,
            src_style=OVERVIEW_STYLE,
            tgt_style=OVERVIEW_STYLE,
            module=module,
            aoe_hint=text.startswith("Your target locks broken"),
        )
    m = BURST_OUT_RE.match(text) or JAM_OUT_RE.match(text)
    if m:
        return RawEvent(
            kind=Kind.JAM,
            src=YOU,
            tgt=_strip_label(m.group("label")),
            src_style=OVERVIEW_STYLE,
            tgt_style=OVERVIEW_STYLE,
            module=m.group("module").strip(),
            aoe_hint="target locks broken" in text,
        )
    m = TACKLE_RE.match(text)
    if m:
        src, tgt = split_tackle(m.group("body"))
        return RawEvent(
            kind=Kind.SCRAM if m.group("what") == "scramble" else Kind.DISRUPT,
            src=src,
            tgt=tgt,
            src_style=OVERVIEW_STYLE,
            tgt_style=OVERVIEW_STYLE,
            module=None,
            observed=YOU not in (src, tgt),
        )
    m = MISS_GROUP_OUT_RE.match(text) or MISS_OUT_RE.match(text)
    if m:
        return RawEvent(
            kind=Kind.MISS,
            src=YOU,
            tgt=m.group("tgt").strip(),
            src_style=DAMAGE_STYLE,
            tgt_style=DAMAGE_STYLE,
            module=m.group("module").strip(),
        )
    m = MISS_DRONE_IN_RE.match(text)
    if m:
        return RawEvent(
            kind=Kind.MISS,
            src=m.group("drone").strip(),
            tgt=YOU,
            src_style=DRONE_STYLE,
            tgt_style=DAMAGE_STYLE,
            module=m.group("module").strip(),
            owner=m.group("owner").strip(),
        )
    m = MISS_IN_RE.match(text)
    if m:
        return RawEvent(
            kind=Kind.MISS,
            src=m.group("src").strip(),
            tgt=YOU,
            src_style=DAMAGE_STYLE,
            tgt_style=DAMAGE_STYLE,
            module=(m.group("module") or "").strip() or None,
        )
    return None


def skeleton(text: str) -> str:
    """A rough shape of an unmatched line, for warnings and the corpus report."""
    text = re.sub(r"^[+-]?\d+", "N", text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>|“[^”]*”", "L", text)
    return re.sub(r"\b[A-Z][\w'.-]*(?: [A-Z0-9][\w'.-]*)*", "X", text)[:80]
