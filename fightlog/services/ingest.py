"""Upload validation and log processing (technical plan §4.1)."""

import gzip
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from allianceauth.services.hooks import get_extension_logger

from fightlog import app_settings
from fightlog.analysis.engagements import engagements_for_log, pilot_keys
from fightlog.analysis.store import BlobTooLarge
from fightlog.catalog import load_catalog
from fightlog.models import CombatLog, CombatLogUpload, Engagement, Fight
from fightlog.parser import PARSER_VERSION, parse_filename, parse_log, read_header
from fightlog.parser.reader import decode

logger = get_extension_logger(__name__)


@dataclass
class UploadResult:
    status: str  # accepted, duplicate, replaced, unchanged, rejected
    message: str
    log: CombatLog | None = None

    @property
    def ok(self) -> bool:
        return self.status != "rejected"


def _dt(epoch: int | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc) if epoch is not None else None


def _owned_character_ids(user) -> set[int]:
    return set(user.character_ownerships.values_list("character__character_id", flat=True))


def _listener_id(user, listener_name: str, filename_id: int | None) -> int | None:
    if filename_id:
        return filename_id
    match = user.character_ownerships.filter(character__character_name=listener_name).first()
    if match:
        return match.character.character_id
    from fightlog.services.resolve import resolve_names

    rn = resolve_names([listener_name]).get(listener_name)
    return rn.character_id if rn else None


def accept_upload(user, filename: str, data: bytes) -> UploadResult:
    if len(data) > app_settings.FIGHTLOG_MAX_UPLOAD_MB * 1024 * 1024:
        return UploadResult("rejected", f"File is larger than {app_settings.FIGHTLOG_MAX_UPLOAD_MB} MB.")
    listener_name, session = read_header(decode(data[:4096]))
    if not listener_name:
        return UploadResult("rejected", "Not a gamelog with a listener (empty character-select session?).")
    filename_session, filename_id = parse_filename(filename)
    session = session or filename_session
    character_id = _listener_id(user, listener_name, filename_id)
    owned = character_id in _owned_character_ids(user) if character_id else False
    if not owned and not user.has_perm("fightlog.upload_any_log"):
        return UploadResult("rejected", f"Listener {listener_name} is not one of your characters.")
    sha = hashlib.sha256(data).hexdigest()
    existing = CombatLog.objects.filter(sha256=sha).first()
    if existing:
        _, created = CombatLogUpload.objects.get_or_create(
            log=existing, user=user, defaults={"original_filename": filename[:255]}
        )
        message = "Already uploaded; credited to you too." if created else "You already uploaded this log."
        return UploadResult("duplicate", message, existing)
    session_dt = _dt(session)
    same = (
        CombatLog.objects.filter(listener_character_id=character_id, session_started_at=session_dt).first()
        if character_id and session_dt
        else None
    )
    if same:
        stored = same.raw_bytes()
        if data.startswith(stored):
            with transaction.atomic():
                detach_log(same)
                same.raw_gz = gzip.compress(data)
                same.raw_size = len(data)
                same.sha256 = sha
                same.status = CombatLog.Status.PENDING
                same.status_message = ""
                same.save()
                CombatLogUpload.objects.get_or_create(
                    log=same, user=user, defaults={"original_filename": filename[:255]}
                )
                _enqueue_processing(same)
            return UploadResult("replaced", "Replaced the shorter copy of this session.", same)
        if stored.startswith(data):
            CombatLogUpload.objects.get_or_create(log=same, user=user, defaults={"original_filename": filename[:255]})
            return UploadResult("unchanged", "A longer copy of this session is already stored.", same)
        return UploadResult("rejected", "A different log for the same session is already stored.")
    with transaction.atomic():
        log = CombatLog.objects.create(
            listener_character_id=character_id,
            listener_name=listener_name[:100],
            session_started_at=session_dt,
            sha256=sha,
            raw_gz=gzip.compress(data),
            raw_size=len(data),
        )
        CombatLogUpload.objects.create(log=log, user=user, original_filename=filename[:255])
        _enqueue_processing(log)
    return UploadResult("accepted", "Queued for processing.", log)


def _enqueue_processing(log: CombatLog) -> None:
    from fightlog import tasks

    transaction.on_commit(lambda: tasks.process_log.apply_async(args=[log.pk], priority=4))


def detach_log(log: CombatLog) -> set[int]:
    """Remove a log's engagements; returns the fights that need a rebuild."""
    fight_ids = set(log.engagements.exclude(fight__isnull=True).values_list("fight_id", flat=True))
    log.engagements.all().delete()
    from fightlog.services.fights import bump_versions

    bump_versions(fight_ids)
    return fight_ids


def process_log(log: CombatLog) -> set[int]:
    """Parse a log into engagements and assign them to fights. Returns affected fight IDs."""
    from fightlog.services.clustering import assign_log_engagements

    affected = detach_log(log) if log.engagements.exists() else set()
    log.status = CombatLog.Status.PARSING
    log.save(update_fields=["status"])
    parsed = parse_log(log.raw_bytes(), load_catalog())
    log.parser_version = PARSER_VERSION
    log.combat_lines = parsed.combat_lines
    log.matched_lines = parsed.matched_lines
    log.warnings = dict(parsed.warnings.most_common(50))
    log.context = [c.to_dict() for c in parsed.context]
    log.label_template = parsed.label_template
    log.first_event_at = _dt(parsed.first_event_t)
    log.last_event_at = _dt(parsed.last_event_t)
    if parsed.listener:
        log.listener_name = parsed.listener[:100]
    if not parsed.is_english:
        log.status = CombatLog.Status.REJECTED
        log.status_message = "This log isn't in English. Set the client language to English to upload logs."
        log.save()
        return affected
    stores = engagements_for_log(
        parsed, log.pk, app_settings.FIGHTLOG_ENGAGEMENT_GAP_SECONDS, app_settings.FIGHTLOG_MIN_PVP_EVENTS
    )
    if not stores:
        log.status = CombatLog.Status.NO_PVP
        log.status_message = "No PvP found."
        log.purge_after = timezone.now() + timedelta(days=app_settings.FIGHTLOG_NO_PVP_RETENTION_DAYS)
        log.save()
        return affected
    try:
        for store in stores:
            Engagement.objects.create(
                log=log,
                start_at=_dt(store.column("t")[0]),
                end_at=_dt(store.column("t")[-1]),
                event_count=len(store),
                actor_keys=pilot_keys(store),
                store=store.encode(app_settings.FIGHTLOG_MAX_BLOB_BYTES),
            )
    except BlobTooLarge as ex:
        log.engagements.all().delete()
        log.status = CombatLog.Status.FAILED
        log.status_message = str(ex)[:255]
        log.save()
        return affected
    log.status = CombatLog.Status.PARSED
    log.status_message = ""
    log.purge_after = None
    log.save()
    affected |= assign_log_engagements(log)
    return affected


def fight_ids_for_log(log: CombatLog) -> list[int]:
    return list(Fight.objects.filter(engagements__log=log).distinct().values_list("pk", flat=True))
