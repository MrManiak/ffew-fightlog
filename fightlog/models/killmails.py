from django.db import models


class Killmail(models.Model):
    """A shared cache of zKillboard killmails, reused across fights."""

    id = models.BigIntegerField(primary_key=True)
    hash = models.CharField(max_length=64)
    time = models.DateTimeField(db_index=True)
    solar_system_id = models.PositiveIntegerField(null=True, blank=True)
    victim_character_id = models.BigIntegerField(null=True, blank=True)
    victim_corporation_id = models.BigIntegerField(null=True, blank=True)
    victim_alliance_id = models.BigIntegerField(null=True, blank=True)
    victim_ship_type_id = models.PositiveIntegerField(null=True, blank=True)
    total_value = models.FloatField(default=0.0)
    is_npc = models.BooleanField(default=False)
    is_solo = models.BooleanField(default=False)
    attackers = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        ordering = ["time"]

    @property
    def zkb_url(self) -> str:
        return f"https://zkillboard.com/kill/{self.id}/"


class FightKillmail(models.Model):
    class Role(models.TextChoices):
        CORE = "core", "Core"
        RELATED = "related", "Related"

    fight = models.ForeignKey("fightlog.Fight", on_delete=models.CASCADE, related_name="fight_killmails")
    killmail = models.ForeignKey(Killmail, on_delete=models.CASCADE, related_name="fight_links")
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.CORE)
    victim_participant = models.ForeignKey(
        "fightlog.Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="losses"
    )

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=["fight", "killmail"], name="fightlog_unique_fight_killmail")]
