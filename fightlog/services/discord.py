"""Discord webhook posts (technical plan §7). Posts only happen when a user asks."""

import time

import requests

from django.conf import settings
from django.utils import timezone

from fightlog import __url__, __version__, app_settings
from fightlog.models import DiscordPost, Fight, Participant


class DiscordRateLimited(RuntimeError):
    def __init__(self, retry_after: float):
        super().__init__(f"Discord rate limit, retry after {retry_after}s")
        self.retry_after = retry_after


def _isk(value: float) -> str:
    for unit, size in (("b", 1e9), ("m", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


def build_message(fight: Fight) -> dict:
    site = getattr(settings, "SITE_URL", "").rstrip("/")
    summary = fight.summary or {}
    fields = []
    for side in fight.sides.all():
        pilots = summary.get("pilots", {}).get(str(side.pk), 0)
        losses = summary.get("losses", {}).get(str(side.pk), 0)
        isk = summary.get("isk_lost", {}).get(str(side.pk), 0.0)
        totals = summary.get("totals", {}).get(str(side.pk), {})
        fields.append(
            {
                "name": side.name,
                "value": (
                    f"{pilots} pilots · {losses} lost ({_isk(isk)} ISK)\n"
                    f"damage dealt {totals.get('damage_out', 0):,} · reps {totals.get('rep_out', 0):,}"
                ),
                "inline": True,
            }
        )
    from .analysis import analysis, default_active_logs

    friendly = fight.sides.filter(order=0).first()
    result = analysis(fight, default_active_logs(fight), None, 30, False)
    ours = [
        p
        for p in result.get("pilots", [])
        if friendly and p.get("side") == friendly.pk and p["kind"] == Participant.Kind.PILOT
    ]
    top_damage = max(ours, key=lambda p: p["damage_out"], default=None)
    top_logi = max(ours, key=lambda p: sum(p["rep_out"].values()), default=None)
    highlights = []
    if top_damage and top_damage["damage_out"]:
        highlights.append(f"Top damage: {top_damage['name']} ({top_damage['damage_out']:,})")
    if top_logi and sum(top_logi["rep_out"].values()):
        highlights.append(f"Top logi: {top_logi['name']} ({sum(top_logi['rep_out'].values()):,} HP)")
    description = f"{fight.start_at:%Y-%m-%d %H:%M}–{fight.end_at:%H:%M} EVE"
    if fight.system:
        description += f" · {fight.system.name}"
    description += f" · {summary.get('logs', 0)} logs"
    if highlights:
        description += "\n" + "\n".join(highlights)
    embed = {
        "title": fight.title[:256],
        "url": f"{site}{fight.get_absolute_url()}",
        "description": description,
        "fields": fields[:25],
        "footer": {"text": f"ffew-fightlog {__version__}"},
        "timestamp": fight.start_at.isoformat(),
    }
    return {"username": "Fight Logs", "embeds": [embed]}


def send(post: DiscordPost) -> DiscordPost:
    webhook = post.webhook
    payload = build_message(post.fight)
    response = requests.post(
        webhook.url,
        params={"wait": "true"},
        json=payload,
        headers={"User-Agent": f"ffew-fightlog/{__version__} (+{__url__})"},
        timeout=app_settings.FIGHTLOG_DISCORD_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        try:
            retry_after = float(response.json().get("retry_after", 5))
        except ValueError:
            retry_after = 5.0
        raise DiscordRateLimited(retry_after)
    if response.status_code >= 400:
        post.status = DiscordPost.Status.FAILED
        post.error = f"HTTP {response.status_code}: {response.text[:500]}"
        post.save(update_fields=["status", "error"])
        return post
    try:
        post.message_id = str(response.json().get("id", ""))[:32]
    except ValueError:
        post.message_id = ""
    post.status = DiscordPost.Status.SENT
    post.error = ""
    post.posted_at = timezone.now()
    post.save(update_fields=["status", "message_id", "error", "posted_at"])
    return post


__all__ = ["DiscordRateLimited", "build_message", "send", "time"]
