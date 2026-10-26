from django.db import models


class SolarSystem(models.Model):
    """Filled lazily from the bundled catalog, with ESI as the fallback for new systems."""

    id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=100, db_index=True)
    security = models.FloatField(default=0.0)
    constellation_id = models.PositiveIntegerField(null=True, blank=True)
    region_id = models.PositiveIntegerField(null=True, blank=True)
    region_name = models.CharField(max_length=100, blank=True, db_index=True)

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return self.name

    @property
    def is_wspace(self) -> bool:
        return 31000000 <= self.id < 32000000
