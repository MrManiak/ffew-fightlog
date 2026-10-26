from django.conf import settings
from django.db import models


class DiscordWebhook(models.Model):
    name = models.CharField(max_length=100, unique=True)
    url = models.URLField(max_length=500, unique=True)
    is_enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return self.name


class DiscordPost(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    fight = models.ForeignKey("fightlog.Fight", on_delete=models.CASCADE, related_name="discord_posts")
    webhook = models.ForeignKey(DiscordWebhook, on_delete=models.CASCADE, related_name="posts")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    posted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED)
    message_id = models.CharField(max_length=32, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-posted_at"]
