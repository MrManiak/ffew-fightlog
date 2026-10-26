import gzip

from django.conf import settings
from django.db import models

from allianceauth.framework.api.user import get_sentinel_user


class CombatLog(models.Model):
    """One uploaded gamelog file."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PARSING = "parsing", "Parsing"
        PARSED = "parsed", "Parsed"
        NO_PVP = "no_pvp", "No PvP found"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"

    listener_character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    listener_name = models.CharField(max_length=100, db_index=True)
    session_started_at = models.DateTimeField(null=True, blank=True)
    first_event_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, unique=True)
    raw_gz = models.BinaryField()
    raw_size = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    status_message = models.CharField(max_length=255, blank=True)
    purge_after = models.DateTimeField(null=True, blank=True)
    parser_version = models.PositiveSmallIntegerField(default=0)
    combat_lines = models.PositiveIntegerField(default=0)
    matched_lines = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=dict, blank=True)
    context = models.JSONField(default=list, blank=True)
    label_template = models.JSONField(default=dict, blank=True)
    clock_offset = models.IntegerField(default=0)
    excluded = models.BooleanField(default=False)
    excluded_reason = models.CharField(max_length=255, blank=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    excluded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["listener_character_id", "session_started_at"], name="fightlog_unique_session"
            )
        ]

    def __str__(self) -> str:
        return (
            f"{self.listener_name} @ {self.session_started_at:%Y-%m-%d %H:%M}"
            if self.session_started_at
            else self.listener_name
        )

    def raw_bytes(self) -> bytes:
        return gzip.decompress(bytes(self.raw_gz))

    @property
    def uploader(self):
        first = self.uploads.order_by("uploaded_at", "pk").select_related("user").first()
        return first.user if first else None


class CombatLogUpload(models.Model):
    """Who uploaded a log. Identical re-uploads add a row instead of a duplicate log."""

    log = models.ForeignKey(CombatLog, on_delete=models.CASCADE, related_name="uploads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET(get_sentinel_user), related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    original_filename = models.CharField(max_length=255, blank=True)

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=["log", "user"], name="fightlog_unique_upload")]


class Engagement(models.Model):
    """One stretch of PvP combat inside one log; the unit that moves between fights."""

    log = models.ForeignKey(CombatLog, on_delete=models.CASCADE, related_name="engagements")
    fight = models.ForeignKey(
        "fightlog.Fight", null=True, blank=True, on_delete=models.SET_NULL, related_name="engagements"
    )
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField(db_index=True)
    event_count = models.PositiveIntegerField(default=0)
    actor_keys = models.JSONField(default=list, blank=True)
    store = models.BinaryField()
    store_version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        default_permissions = ()
        ordering = ["start_at", "pk"]
