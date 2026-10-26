"""Non-combat lines that matter to the analysis: fleets, systems, reships, kills, wormholes."""

import re

from .events import ContextEvent, ContextKind
from .markup import clean, showinfo_links

JUMP_RE = re.compile(r"^Jumping from (?P<a>.+) to (?P<b>.+?)\.?$")
UNDOCK_RE = re.compile(r"^Undocking from (?P<station>.+) to (?P<system>.+) solar system\.?$")
FLEET_INVITE_RE = re.compile(r"wants you to join their fleet")
FLEET_JOIN_RE = re.compile(r"join the fleet commanded by")
FLEET_INVITED_RE = re.compile(r"^(?P<name>.+) has been invited to your fleet")
FOLLOW_RE = re.compile(r"^Following (?P<name>.+) in warp$")
REGROUP_RE = re.compile(r"^Regrouping to (?P<name>.+)$")
EXPLODE_RE = re.compile(r"\bas (?P<victim>.+) begins to explode\.?$")
WSPACE_RE = re.compile(r"^Local spatial phenomena may cause strange effects")
WORMHOLE_RE = re.compile(
    r"entering a wormhole|pass through the wormhole|nearby Wormhole|caused by the wormhole|of the nearby Wormhole",
    re.I,
)


def match_context(t: int, kind: str, msg: str, line_no: int) -> ContextEvent | None:
    if kind == "None":
        text = clean(msg)
        m = JUMP_RE.match(text)
        if m:
            return ContextEvent(t, ContextKind.SYSTEM, system=m.group("b").strip(), line_no=line_no)
        m = UNDOCK_RE.match(text)
        if m:
            return ContextEvent(t, ContextKind.SYSTEM, system=m.group("system").strip(), line_no=line_no)
        return None
    if kind == "question":
        if FLEET_INVITE_RE.search(msg) or FLEET_JOIN_RE.search(msg):
            links = showinfo_links(msg)
            if links:
                _, item_id, text = links[0]
                ctx = ContextKind.FLEET_INVITE if FLEET_INVITE_RE.search(msg) else ContextKind.FLEET_JOIN
                return ContextEvent(t, ctx, name=text.strip(), character_id=item_id, line_no=line_no)
        return None
    if kind != "notify":
        return None
    text = clean(msg)
    m = FOLLOW_RE.match(text)
    if m:
        return ContextEvent(t, ContextKind.FLEET_WARP, name=m.group("name").strip(), line_no=line_no)
    m = REGROUP_RE.match(text)
    if m:
        return ContextEvent(t, ContextKind.REGROUP, name=m.group("name").strip(), line_no=line_no)
    m = FLEET_INVITED_RE.match(text)
    if m:
        return ContextEvent(t, ContextKind.FLEET_MEMBER, name=m.group("name").strip(), line_no=line_no)
    if text.startswith("Disembarking from ship"):
        return ContextEvent(t, ContextKind.RESHIP, line_no=line_no)
    if text.startswith("Starting clone jumping"):
        return ContextEvent(t, ContextKind.CLONE_JUMP, line_no=line_no)
    m = EXPLODE_RE.search(text)
    if m:
        return ContextEvent(t, ContextKind.KILL_NOTICE, name=m.group("victim").strip(), line_no=line_no)
    if WSPACE_RE.match(text):
        return ContextEvent(t, ContextKind.WSPACE_HINT, line_no=line_no)
    if WORMHOLE_RE.search(text):
        return ContextEvent(t, ContextKind.WORMHOLE_HINT, line_no=line_no)
    return None
