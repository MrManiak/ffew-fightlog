from django.db import models


class ResolvedName(models.Model):
    """ESI name → character and affiliation cache. Only names taken from labels go here."""

    name = models.CharField(max_length=100, unique=True)
    character_id = models.BigIntegerField(null=True, blank=True)
    corporation_id = models.BigIntegerField(null=True, blank=True)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    resolved_at = models.DateTimeField()

    class Meta:
        default_permissions = ()


class Corporation(models.Model):
    id = models.BigIntegerField(primary_key=True)
    ticker = models.CharField(max_length=10)
    name = models.CharField(max_length=255)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    fetched_at = models.DateTimeField()

    class Meta:
        default_permissions = ()


class Alliance(models.Model):
    id = models.BigIntegerField(primary_key=True)
    ticker = models.CharField(max_length=10)
    name = models.CharField(max_length=255)
    fetched_at = models.DateTimeField()

    class Meta:
        default_permissions = ()
