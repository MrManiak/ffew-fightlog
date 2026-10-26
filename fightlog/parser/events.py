"""Data types produced by the combat-log parser."""

from collections import Counter
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag


class Kind(IntEnum):
    DAMAGE = 0
    MISS = 1
    REP_SHIELD = 2
    REP_ARMOR = 3
    REP_HULL = 4
    CAP = 5
    NEUT = 6
    NOS = 7
    JAM = 8
    SCRAM = 9
    DISRUPT = 10


REP_KINDS = frozenset({Kind.REP_SHIELD, Kind.REP_ARMOR, Kind.REP_HULL})
FRIENDLY_KINDS = REP_KINDS | {Kind.CAP}
HOSTILE_KINDS = frozenset({Kind.DAMAGE, Kind.NEUT, Kind.NOS, Kind.JAM, Kind.SCRAM, Kind.DISRUPT})
TACKLE_KINDS = frozenset({Kind.SCRAM, Kind.DISRUPT})


class Quality(IntEnum):
    NONE = 0
    GRAZES = 1
    GLANCES_OFF = 2
    HITS = 3
    PENETRATES = 4
    SMASHES = 5
    WRECKS = 6


QUALITY_WORDS = {
    "Grazes": Quality.GRAZES,
    "Glances Off": Quality.GLANCES_OFF,
    "Hits": Quality.HITS,
    "Penetrates": Quality.PENETRATES,
    "Smashes": Quality.SMASHES,
    "Wrecks": Quality.WRECKS,
}


class Flag(IntFlag):
    NONE = 0
    AOE = 1  # smartbombs, bombs, burst jammers: hits everything nearby
    DRONE = 2  # the source used a drone or fighter
    OBSERVED = 4  # third-party event: neither end is the listener
    NON_SHIP_TARGET = 8  # drones, missiles in flight, deployables, structures
    CAN_MISS = 16  # turrets, drones, fighters


class ActorKind:
    LISTENER = "listener"
    PILOT = "pilot"
    SHIP_ONLY = "ship_only"
    NPC = "npc"
    DRONE = "drone"
    STRUCTURE = "structure"
    CHARGE = "charge"
    UNKNOWN = "unknown"
    EMPTY = "empty"

    PLAYER_KINDS = frozenset({LISTENER, PILOT, SHIP_ONLY, DRONE, STRUCTURE, CHARGE})
    NON_SHIP = frozenset({DRONE, STRUCTURE, CHARGE})


@dataclass(frozen=True)
class Actor:
    kind: str
    name: str | None = None
    ship: str | None = None
    corp: str | None = None
    alliance: str | None = None
    owner: str | None = None
    raw: str = ""

    @property
    def is_player(self) -> bool:
        if self.kind in (ActorKind.DRONE, ActorKind.STRUCTURE, ActorKind.CHARGE):
            return bool(self.corp or self.alliance or self.owner)
        return self.kind in ActorKind.PLAYER_KINDS


LISTENER_PLACEHOLDER = Actor(ActorKind.LISTENER, raw="you")


@dataclass
class Event:
    t: int
    kind: Kind
    src: Actor
    tgt: Actor
    module: str | None
    amount: int
    quality: Quality
    flags: Flag
    line_no: int


class ContextKind:
    SYSTEM = "system"
    FLEET_INVITE = "fleet_invite"
    FLEET_JOIN = "fleet_join"
    FLEET_MEMBER = "fleet_member"
    FLEET_WARP = "fleet_warp"
    REGROUP = "regroup"
    RESHIP = "reship"
    CLONE_JUMP = "clone_jump"
    KILL_NOTICE = "kill_notice"
    WSPACE_HINT = "wspace_hint"
    WORMHOLE_HINT = "wormhole_hint"

    FLEET_KINDS = frozenset({FLEET_INVITE, FLEET_JOIN, FLEET_MEMBER, FLEET_WARP, REGROUP})
    FLEET_START_KINDS = frozenset({FLEET_INVITE, FLEET_JOIN})


@dataclass(frozen=True)
class ContextEvent:
    t: int
    kind: str
    name: str | None = None
    character_id: int | None = None
    system: str | None = None
    line_no: int = 0

    def to_dict(self) -> dict:
        data = {"t": self.t, "kind": self.kind, "line": self.line_no}
        for key in ("name", "character_id", "system"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ContextEvent":
        return cls(
            t=data["t"],
            kind=data["kind"],
            name=data.get("name"),
            character_id=data.get("character_id"),
            system=data.get("system"),
            line_no=data.get("line", 0),
        )


@dataclass
class ParsedLog:
    listener: str | None
    session_started: int | None
    events: list[Event] = field(default_factory=list)
    context: list[ContextEvent] = field(default_factory=list)
    warnings: Counter = field(default_factory=Counter)
    line_count: int = 0
    combat_lines: int = 0
    matched_lines: int = 0
    label_template: dict = field(default_factory=dict)
    label_shapes: Counter = field(default_factory=Counter)

    @property
    def is_english(self) -> bool:
        """Non-English clients localize message text; almost nothing matches."""
        if self.combat_lines < 20:
            return True
        return self.matched_lines / self.combat_lines >= 0.2

    @property
    def first_event_t(self) -> int | None:
        return self.events[0].t if self.events else None

    @property
    def last_event_t(self) -> int | None:
        return self.events[-1].t if self.events else None
