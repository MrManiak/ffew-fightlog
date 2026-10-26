from django.db import models


class General(models.Model):
    """A meta model for app permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", "Can browse fights and upload own logs"),
            ("upload_any_log", "Can upload logs for characters they don't own"),
            ("curate_fights", "Can edit any fight, delete any log and manage allies"),
            ("post_to_discord", "Can post fights to Discord"),
        )
