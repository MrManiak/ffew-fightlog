"""Combat-log parser. Pure Python: no Django imports anywhere in this package."""

from .events import Actor, ActorKind, ContextEvent, ContextKind, Event, Flag, Kind, ParsedLog, Quality
from .reader import parse_filename, parse_log, read_header

# Bump whenever a pattern or classification rule changes; stored logs with an older
# version are re-parsed by reparse_all.
PARSER_VERSION = 1

__all__ = [
    "PARSER_VERSION",
    "Actor",
    "ActorKind",
    "ContextEvent",
    "ContextKind",
    "Event",
    "Flag",
    "Kind",
    "ParsedLog",
    "Quality",
    "parse_filename",
    "parse_log",
    "read_header",
]
