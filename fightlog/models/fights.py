from django.conf import settings
from django.db import models
from django.urls import reverse


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        default_permissions = ()
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Fight(models.Model):
    class SystemSource(models.TextChoices):
        NONE = "", "Unknown"
        MANUAL = "manual", "Curator"
        ZKB = "zkb", "zKillboard"
        LOG = "log", "Log"
        WSPACE_UNKNOWN = "wspace_unknown", "J-space, system unknown"

    title = models.CharField(max_length=200)
    title_is_manual = models.BooleanField(default=False)
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField(db_index=True)
    system = models.ForeignKey(
        "fightlog.SolarSystem", null=True, blank=True, on_delete=models.SET_NULL, related_name="fights"
    )
    system_source = models.CharField(max_length=16, choices=SystemSource.choices, default="", blank=True)
    notes = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="fights")
    is_curated = models.BooleanField(default=False)
    data_version = models.PositiveIntegerField(default=1)
    built_version = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)
    zkb_window_start = models.DateTimeField(null=True, blank=True)
    zkb_window_end = models.DateTimeField(null=True, blank=True)
    zkb_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        ordering = ["-start_at"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("fightlog:fight", args=[self.pk])

    @property
    def duration_seconds(self) -> int:
        return int((self.end_at - self.start_at).total_seconds())

    @property
    def is_built(self) -> bool:
        return self.built_version >= self.data_version


class FightRedirect(models.Model):
    """An absorbed fight's ID, so old links and Discord posts still resolve."""

    old_id = models.PositiveBigIntegerField(primary_key=True)
    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="redirects")

    class Meta:
        default_permissions = ()


class FightData(models.Model):
    fight = models.OneToOneField(Fight, on_delete=models.CASCADE, related_name="data")
    actors = models.JSONField(default=list, blank=True)
    event_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)
    built_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()


class FightDataChunk(models.Model):
    fight_data = models.ForeignKey(FightData, on_delete=models.CASCADE, related_name="chunks")
    seq = models.PositiveIntegerField()
    store = models.BinaryField()

    class Meta:
        default_permissions = ()
        ordering = ["seq"]
        constraints = [models.UniqueConstraint(fields=["fight_data", "seq"], name="fightlog_unique_chunk")]


class Side(models.Model):
    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="sides")
    name = models.CharField(max_length=100)
    is_friendly = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)
    name_is_manual = models.BooleanField(default=False)

    class Meta:
        default_permissions = ()
        ordering = ["order", "pk"]

    def __str__(self) -> str:
        return self.name


class Participant(models.Model):
    class Kind(models.TextChoices):
        PILOT = "pilot", "Pilot"
        NPC = "npc", "NPC"
        STRUCTURE = "structure", "Structure"
        DRONE = "drone", "Drone"
        SHIP_ONLY = "ship_only", "Unknown pilot"

    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="participants")
    key = models.CharField(max_length=255)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PILOT)
    character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    corporation_id = models.BigIntegerField(null=True, blank=True)
    corporation_ticker = models.CharField(max_length=10, blank=True, db_index=True)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    alliance_ticker = models.CharField(max_length=10, blank=True, db_index=True)
    side = models.ForeignKey(Side, null=True, blank=True, on_delete=models.SET_NULL, related_name="participants")
    side_source = models.CharField(max_length=8, default="auto")
    side_confidence = models.FloatField(default=0.0)
    hint = models.CharField(max_length=32, blank=True)
    attributed_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="attributed_actors"
    )
    attribution_source = models.CharField(max_length=16, blank=True)
    has_log = models.BooleanField(default=False)
    is_aa_member = models.BooleanField(default=False)
    ships = models.CharField(max_length=255, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["fight", "key"], name="fightlog_unique_participant")]

    def __str__(self) -> str:
        return self.name


class ParticipantShip(models.Model):
    class Source(models.TextChoices):
        PEER_LOG = "peer_log", "Another log"
        KILLMAIL = "killmail", "Killmail"
        INFERRED = "inferred", "Inferred"

    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="ship_rows")
    type_id = models.PositiveIntegerField(null=True, blank=True)
    type_name = models.CharField(max_length=100, db_index=True)
    source = models.CharField(max_length=16, choices=Source.choices)
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()


class AttributionOverride(models.Model):
    """A curator's "this Loki was pilot X", keyed on raw log data so it survives rebuilds."""

    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="attribution_overrides")
    log = models.ForeignKey("fightlog.CombatLog", on_delete=models.CASCADE, related_name="+")
    polarity = models.CharField(max_length=16)
    raw_label = models.CharField(max_length=255)
    participant_key = models.CharField(max_length=255)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["fight", "log", "polarity", "raw_label"], name="fightlog_unique_attribution"
            )
        ]


class SideOverride(models.Model):
    """A curator's side choice for one participant, kept across rebuilds."""

    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="side_overrides")
    participant_key = models.CharField(max_length=255)
    side = models.ForeignKey(Side, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["fight", "participant_key"], name="fightlog_unique_side_override")
        ]


class FightChange(models.Model):
    class Action(models.TextChoices):
        RENAME = "rename", "Renamed"
        MERGE = "merge", "Merged"
        SPLIT = "split", "Split"
        MOVE = "move", "Moved an engagement"
        SIDE = "side", "Changed a side"
        SIDE_RENAME = "side_rename", "Renamed a side"
        ATTRIBUTE = "attribute", "Attributed an actor"
        EXCLUDE = "exclude", "Excluded a log"
        INCLUDE = "include", "Re-included a log"
        DELETE_LOG = "delete_log", "Deleted a log"
        SYSTEM = "system", "Set the system"
        NOTES = "notes", "Edited notes"
        TAGS = "tags", "Edited tags"
        DISCORD = "discord", "Posted to Discord"

    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="changes")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-at", "-pk"]


class FriendlyEntity(models.Model):
    class EntityType(models.TextChoices):
        ALLIANCE = "alliance", "Alliance"
        CORPORATION = "corporation", "Corporation"

    entity_type = models.CharField(max_length=12, choices=EntityType.choices)
    entity_id = models.BigIntegerField()
    name = models.CharField(max_length=255, blank=True)
    ticker = models.CharField(max_length=10, blank=True)

    class Meta:
        default_permissions = ()
        verbose_name_plural = "friendly entities"
        constraints = [models.UniqueConstraint(fields=["entity_type", "entity_id"], name="fightlog_unique_friendly")]

    def __str__(self) -> str:
        return f"{self.name or self.entity_id} ({self.get_entity_type_display()})"
