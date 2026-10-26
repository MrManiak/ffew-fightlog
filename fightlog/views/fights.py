from datetime import datetime, time
from datetime import timezone as dt_timezone

from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from fightlog import app_settings
from fightlog.forms import FightSearchForm
from fightlog.models import DiscordWebhook, Fight, FightRedirect, Participant, Side
from fightlog.services.analysis import default_side, fight_logs
from fightlog.services.fights import can_edit


def _search(form: FightSearchForm):
    fights = Fight.objects.select_related("system").prefetch_related("tags")
    if not form.is_valid():
        return fights
    data = form.cleaned_data
    if data["date_from"]:
        fights = fights.filter(start_at__gte=datetime.combine(data["date_from"], time.min, dt_timezone.utc))
    if data["date_to"]:
        fights = fights.filter(start_at__lte=datetime.combine(data["date_to"], time.max, dt_timezone.utc))
    if data["system"]:
        fights = fights.filter(Q(system__name__iexact=data["system"]) | Q(system__region_name__iexact=data["system"]))
    if data["who"]:
        who = data["who"]
        fights = fights.filter(
            Q(participants__name__iexact=who)
            | Q(participants__corporation_ticker__iexact=who)
            | Q(participants__alliance_ticker__iexact=who)
        )
    if data["ship"]:
        fights = fights.filter(participants__ship_rows__type_name__iexact=data["ship"])
    if data["uploader"]:
        fights = fights.filter(
            Q(engagements__log__uploads__user__username__iexact=data["uploader"])
            | Q(engagements__log__uploads__user__profile__main_character__character_name__iexact=data["uploader"])
        )
    if data["text"]:
        text = data["text"].lstrip("#")
        fights = fights.filter(Q(title__icontains=text) | Q(notes__icontains=text) | Q(tags__name__iexact=text))
    if data["min_pilots"]:
        fights = fights.annotate(n_pilots=Count("participants", filter=Q(participants__kind="pilot"), distinct=True))
        fights = fights.filter(n_pilots__gte=data["min_pilots"])
    if data["ours"]:
        fights = fights.filter(participants__is_aa_member=True)
    return fights.distinct()


@login_required
@permission_required("fightlog.basic_access")
def fight_list(request):
    form = FightSearchForm(request.GET or None)
    fights = _search(form).order_by("-start_at")
    page = Paginator(fights, 25).get_page(request.GET.get("page"))
    rows = []
    for fight in page:
        summary = fight.summary or {}
        sides = list(fight.sides.all())
        rows.append(
            {
                "fight": fight,
                "sides": [
                    {
                        "side": s,
                        "pilots": summary.get("pilots", {}).get(str(s.pk), 0),
                        "losses": summary.get("losses", {}).get(str(s.pk), 0),
                        "isk": summary.get("isk_lost", {}).get(str(s.pk), 0.0),
                    }
                    for s in sides
                ],
                "logs": summary.get("logs", 0),
            }
        )
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "fightlog/fight_list.html",
        {"form": form, "page": page, "rows": rows, "query": query.urlencode()},
    )


def _get_fight(fight_id: int):
    fight = Fight.objects.select_related("system").filter(pk=fight_id).first()
    if fight is None:
        redirect_row = FightRedirect.objects.filter(old_id=fight_id).first()
        if redirect_row:
            return None, redirect_row.fight
        raise Http404("No such fight")
    return fight, None


@login_required
@permission_required("fightlog.basic_access")
def fight_detail(request, fight_id: int):
    fight, moved = _get_fight(fight_id)
    if moved:
        return redirect(moved.get_absolute_url(), permanent=True)
    logs = fight_logs(fight)
    for log in logs:
        log.uploader_names = ", ".join(
            (
                u.user.profile.main_character.character_name
                if getattr(u.user, "profile", None) and u.user.profile.main_character
                else u.user.username
            )
            for u in log.uploads.select_related("user__profile__main_character")
        )
    sides = list(fight.sides.all())
    participants = list(
        fight.participants.filter(kind__in=[Participant.Kind.PILOT, Participant.Kind.SHIP_ONLY])
        .select_related("side", "attributed_to")
        .order_by("name")
    )
    pilots = [p for p in participants if p.kind == Participant.Kind.PILOT]
    unknown_actors = [p for p in participants if p.kind == Participant.Kind.SHIP_ONLY and not p.attributed_to_id]
    editable = can_edit(request.user, fight)
    default = default_side(fight)
    fight_config = {
        "id": fight.pk,
        "built": fight.is_built,
        "analysisUrl": reverse("fightlog:api_analysis", args=[fight.pk]),
        "eventsUrl": reverse("fightlog:api_events", args=[fight.pk]),
        "pilotUrl": reverse("fightlog:api_pilot", args=[fight.pk]),
        "statusUrl": reverse("fightlog:api_fight_status", args=[fight.pk]),
        "defaultPerspective": f"side:{default.pk}" if default else "",
        "logs": [{"id": log.pk, "listener": log.listener_name, "excluded": log.excluded} for log in logs],
        "sides": [{"id": s.pk, "name": s.name, "friendly": s.is_friendly, "order": s.order} for s in sides],
    }
    context = {
        "fight_config": fight_config,
        "fight": fight,
        "sides": sides,
        "logs": logs,
        "pilots": pilots,
        "unknown_actors": unknown_actors,
        "kills": fight.fight_killmails.select_related("killmail", "victim_participant").order_by("killmail__time"),
        "changes": fight.changes.select_related("user")[:50],
        "can_edit": editable,
        "can_post": request.user.has_perm("fightlog.post_to_discord"),
        "webhooks": (
            DiscordWebhook.objects.filter(is_enabled=True) if request.user.has_perm("fightlog.post_to_discord") else []
        ),
        "discord_posts": fight.discord_posts.select_related("webhook", "user")[:10],
        "other_fights": (
            Fight.objects.exclude(pk=fight.pk).filter(start_at__lte=fight.end_at, end_at__gte=fight.start_at)[:20]
            if editable
            else []
        ),
        "summary": fight.summary or {},
        "side_choices": sides + [Side(pk=0, name="Unknown")],
        "engagements": fight.engagements.select_related("log").order_by("start_at"),
        "settings": {"analysis_cache": app_settings.FIGHTLOG_ANALYSIS_CACHE_SECONDS},
    }
    return render(request, "fightlog/fight_detail.html", context)
