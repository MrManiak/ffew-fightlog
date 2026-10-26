"""Who is a bare name? (technical plan §4.3, classification rules 2–6).

ESI name lookups are never used here: NPC names such as "Emergent Warden" are also
real player characters.
"""

from collections import Counter

from fightlog.catalog import Catalog

from .events import Actor, ActorKind


class BareNameClassifier:
    def __init__(self, catalog: Catalog, known_pilots: dict[str, Actor]):
        self.catalog = catalog
        self.known_pilots = known_pilots
        self.npc_votes: Counter = Counter()  # incoming damage with no weapon segment
        self.player_votes: Counter = Counter()  # incoming fire from a player module

    def note_incoming(self, name: str, weapon: str | None) -> None:
        if not weapon:
            self.npc_votes[name] += 1
        elif self.catalog.is_player_module(weapon) and not self.catalog.is_npc_name(name):
            self.player_votes[name] += 1

    def classify(self, name: str) -> Actor:
        if not name:
            return Actor(ActorKind.EMPTY, raw=name)
        if name in self.known_pilots:
            return self.known_pilots[name]
        if self.npc_votes[name]:
            return Actor(ActorKind.NPC, name=name, raw=name)
        if self.catalog.is_npc_name(name):
            return Actor(ActorKind.NPC, name=name, raw=name)
        if self.catalog.is_charge_group(name):
            # "to Light Missile": a smartbomb hitting a missile in flight, owner unknown.
            return Actor(ActorKind.CHARGE, name=name, raw=name)
        if self.player_votes[name] and not self.catalog.is_known(name):
            return Actor(ActorKind.PILOT, name=name, raw=name)
        if self.catalog.is_known(name):
            # A bare hull name in a damage line is an NPC named like a ship
            # (players are always labelled there), e.g. "Orthrus" in a Sleeper site.
            return Actor(ActorKind.NPC, name=name, raw=name)
        return Actor(ActorKind.UNKNOWN, name=name, raw=name)

    def demote_ship_only(self, actor: Actor) -> Actor:
        """A ship-type-only label whose name also acts like an NPC in this log is that NPC."""
        if actor.kind == ActorKind.SHIP_ONLY and actor.ship and self.npc_votes[actor.ship]:
            return Actor(ActorKind.NPC, name=actor.ship, raw=actor.raw)
        return actor
