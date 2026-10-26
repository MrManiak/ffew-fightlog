"""App settings. Every value can be overridden in the Auth's local.py."""

from app_utils.app_settings import clean_setting

FIGHTLOG_MAX_UPLOAD_MB = clean_setting("FIGHTLOG_MAX_UPLOAD_MB", 25, min_value=1, max_value=200)
"""Largest gamelog file accepted, in MB."""

FIGHTLOG_UPLOAD_DEFAULT_DAYS = clean_setting("FIGHTLOG_UPLOAD_DEFAULT_DAYS", 3, min_value=1, max_value=3650)
"""Default session date range on the upload page."""

FIGHTLOG_NO_PVP_RETENTION_DAYS = clean_setting("FIGHTLOG_NO_PVP_RETENTION_DAYS", 30, min_value=0, max_value=3650)
"""How long logs without PvP are kept (hidden), so parser upgrades can still find PvP in them."""

FIGHTLOG_FRIENDLY_LABEL = clean_setting("FIGHTLOG_FRIENDLY_LABEL", "")
"""Name of our side in fight titles. Empty: the friendly side's largest alliance ticker."""

FIGHTLOG_ENGAGEMENT_GAP_SECONDS = clean_setting("FIGHTLOG_ENGAGEMENT_GAP_SECONDS", 300, min_value=30)
"""A quiet gap longer than this starts a new engagement."""

FIGHTLOG_MERGE_TOLERANCE_SECONDS = clean_setting("FIGHTLOG_MERGE_TOLERANCE_SECONDS", 120, min_value=0)
"""How far apart two engagements may be and still join the same fight."""

FIGHTLOG_MIN_PVP_EVENTS = clean_setting("FIGHTLOG_MIN_PVP_EVENTS", 5, min_value=1)
"""Engagements with fewer PvP events than this are dropped."""

FIGHTLOG_CLOCK_OFFSET_SEARCH_SECONDS = clean_setting(
    "FIGHTLOG_CLOCK_OFFSET_SEARCH_SECONDS", 10, min_value=0, max_value=120
)
"""Largest clock offset searched for between two logs."""

FIGHTLOG_DEDUPE_TOLERANCE_SECONDS = clean_setting("FIGHTLOG_DEDUPE_TOLERANCE_SECONDS", 2, min_value=0, max_value=10)
"""Two logs' copies of one event may be this far apart after offsets."""

FIGHTLOG_SIDE_MIN_CONFIDENCE = clean_setting("FIGHTLOG_SIDE_MIN_CONFIDENCE", 0.3, required_type=float)
"""Groups placed with less confidence than this go to Unknown."""

FIGHTLOG_ZKB_ENABLED = clean_setting("FIGHTLOG_ZKB_ENABLED", True)
"""Link fights to zKillboard killmails."""

FIGHTLOG_ZKB_REQUEST_DELAY_MS = clean_setting("FIGHTLOG_ZKB_REQUEST_DELAY_MS", 1000, min_value=100)
"""Minimum delay between two zKillboard requests, across all workers."""

FIGHTLOG_ZKB_MAX_ENTITY_QUERIES = clean_setting("FIGHTLOG_ZKB_MAX_ENTITY_QUERIES", 8, min_value=1, max_value=50)
"""How many characters, corps or alliances one fight may query zKillboard for."""

FIGHTLOG_ZKB_EXPANSION_MINUTES = clean_setting("FIGHTLOG_ZKB_EXPANSION_MINUTES", 15, min_value=0, max_value=120)
"""Kills this close to a fight that share two pilots with it join as related kills."""

FIGHTLOG_ZKB_EXPANSION_ROUNDS = clean_setting("FIGHTLOG_ZKB_EXPANSION_ROUNDS", 2, min_value=0, max_value=5)
"""How many times the killmail window may widen."""

FIGHTLOG_ANALYSIS_CACHE_SECONDS = clean_setting("FIGHTLOG_ANALYSIS_CACHE_SECONDS", 86400, min_value=0)
"""How long an analysis view stays cached."""

FIGHTLOG_STORE_CHUNK_EVENTS = clean_setting("FIGHTLOG_STORE_CHUNK_EVENTS", 100000, min_value=1000)
"""Events per FightDataChunk row."""

FIGHTLOG_MAX_BLOB_BYTES = clean_setting("FIGHTLOG_MAX_BLOB_BYTES", 8388608, min_value=65536)
"""Largest compressed blob written to the database (half MariaDB's default max_allowed_packet)."""

FIGHTLOG_DISCORD_TIMEOUT_SECONDS = clean_setting("FIGHTLOG_DISCORD_TIMEOUT_SECONDS", 10, min_value=1)
"""HTTP timeout for Discord webhook posts."""
