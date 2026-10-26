from django.contrib import admin, messages

from .models import CombatLog, DiscordPost, DiscordWebhook, Fight, FightChange, FriendlyEntity


@admin.register(DiscordWebhook)
class DiscordWebhookAdmin(admin.ModelAdmin):
    list_display = ("name", "is_enabled")
    list_filter = ("is_enabled",)


@admin.register(FriendlyEntity)
class FriendlyEntityAdmin(admin.ModelAdmin):
    list_display = ("name", "ticker", "entity_type", "entity_id")
    list_filter = ("entity_type",)


@admin.register(Fight)
class FightAdmin(admin.ModelAdmin):
    list_display = ("title", "start_at", "system", "is_curated", "data_version", "built_version")
    search_fields = ("title",)
    readonly_fields = ("summary", "data_version", "built_version", "created_at", "updated_at")
    exclude = ("tags",)
    actions = ["rebuild"]

    @admin.action(description="Rebuild selected fights")
    def rebuild(self, request, queryset):
        from .services.fights import bump_versions, queue_rebuilds

        ids = list(queryset.values_list("pk", flat=True))
        bump_versions(ids)
        queue_rebuilds(ids, countdown=1)
        messages.info(request, f"Queued {len(ids)} rebuilds.")


@admin.register(CombatLog)
class CombatLogAdmin(admin.ModelAdmin):
    list_display = ("listener_name", "session_started_at", "status", "parser_version", "excluded")
    list_filter = ("status", "excluded")
    search_fields = ("listener_name",)
    exclude = ("raw_gz",)
    readonly_fields = ("sha256", "raw_size", "warnings", "context", "label_template", "clock_offset")
    actions = ["reparse"]

    @admin.action(description="Re-parse selected logs")
    def reparse(self, request, queryset):
        from .tasks import process_log

        for log_id in queryset.values_list("pk", flat=True):
            process_log.apply_async(args=[log_id], priority=6)
        messages.info(request, "Queued for re-parsing.")


@admin.register(FightChange)
class FightChangeAdmin(admin.ModelAdmin):
    list_display = ("fight", "action", "user", "at")
    list_filter = ("action",)


@admin.register(DiscordPost)
class DiscordPostAdmin(admin.ModelAdmin):
    list_display = ("fight", "webhook", "status", "posted_at", "user")
    list_filter = ("status",)
