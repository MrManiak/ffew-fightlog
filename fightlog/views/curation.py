from datetime import datetime
from datetime import timezone as dt_timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from fightlog.forms import FriendlyEntityForm
from fightlog.models import CombatLog, DiscordPost, DiscordWebhook, Engagement, Fight, FightChange, FriendlyEntity, Side
from fightlog.services import curation
from fightlog.services.fights import can_edit, record_change

EDIT_ACTIONS = {
    "rename",
    "notes",
    "tags",
    "system",
    "side",
    "side_rename",
    "attribute",
    "exclude",
    "include",
    "split",
    "merge",
    "move",
}


def _parse_dt(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=dt_timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


@login_required
@permission_required("fightlog.basic_access")
@require_POST
def edit(request, fight_id: int, action: str):
    fight = get_object_or_404(Fight, pk=fight_id)
    if action not in EDIT_ACTIONS:
        return HttpResponseForbidden("Unknown action")
    if not can_edit(request.user, fight):
        return HttpResponseForbidden("You can't edit this fight.")
    post = request.POST
    user = request.user
    target_fight = fight
    if action == "rename":
        curation.rename(fight, post.get("title", "").strip() or fight.title, user)
    elif action == "notes":
        curation.set_notes(fight, post.get("notes", ""), user)
    elif action == "tags":
        curation.set_tags(fight, post.get("tags", "").replace(",", " ").split(), user)
    elif action == "system":
        if not curation.set_system(fight, post.get("system", ""), user):
            messages.error(request, "No solar system with that name.")
    elif action == "side":
        keys = post.getlist("keys")
        side_id = post.get("side")
        side = None if side_id in (None, "", "0") else get_object_or_404(Side, pk=side_id, fight=fight)
        if keys:
            curation.set_side(fight, keys, side, user)
    elif action == "side_rename":
        side = get_object_or_404(Side, pk=post.get("side"), fight=fight)
        curation.rename_side(side, post.get("name", "").strip() or side.name, user)
    elif action == "attribute":
        if not curation.attribute(fight, post.get("actor", ""), post.get("pilot", ""), user):
            messages.error(request, "That actor can't be attributed.")
    elif action in ("exclude", "include"):
        log = get_object_or_404(CombatLog.objects.defer("raw_gz"), pk=post.get("log"), engagements__fight=fight)
        curation.set_excluded(log, action == "exclude", post.get("reason", ""), user)
    elif action == "split":
        at = _parse_dt(post.get("at", ""))
        if at is None or not fight.start_at < at < fight.end_at:
            messages.error(request, "Pick a split time inside the fight.")
        else:
            target_fight = fight
            new = curation.split_fight(fight, at, user)
            messages.success(request, f"Split off fight #{new.pk}.")
    elif action == "merge":
        other = get_object_or_404(Fight, pk=post.get("other"))
        if not can_edit(user, other):
            return HttpResponseForbidden("Merging needs edit rights on both fights.")
        target_fight = curation.merge_fights([fight, other], user)
    elif action == "move":
        engagement = get_object_or_404(Engagement, pk=post.get("engagement"), fight=fight)
        target = get_object_or_404(Fight, pk=post.get("target"))
        if not can_edit(user, target):
            return HttpResponseForbidden("Moving needs edit rights on both fights.")
        curation.move_engagement(engagement, target, user)
    return redirect(target_fight.get_absolute_url())


@login_required
@permission_required("fightlog.post_to_discord")
@require_POST
def discord_post(request, fight_id: int):
    from fightlog.tasks import post_fight_to_discord

    fight = get_object_or_404(Fight, pk=fight_id)
    webhook = get_object_or_404(DiscordWebhook, pk=request.POST.get("webhook"), is_enabled=True)
    post = DiscordPost.objects.create(fight=fight, webhook=webhook, user=request.user)
    record_change(fight, request.user, FightChange.Action.DISCORD, curates=False, webhook=webhook.name)
    post_fight_to_discord.apply_async(args=[post.pk], priority=3)
    messages.info(request, f"Posting to {webhook.name}…")
    return redirect(fight.get_absolute_url())


def _resolve_entity(entity_type: str, identifier: str) -> tuple[int, str, str] | None:
    from fightlog.services import resolve

    identifier = identifier.strip()
    if identifier.isdigit():
        entity_id = int(identifier)
    else:
        try:
            data = resolve.esi_universe_ids([identifier])
        except Exception:  # noqa: BLE001
            return None
        rows = data.get("alliances" if entity_type == "alliance" else "corporations") or []
        if not rows:
            return None
        entity_id = rows[0]["id"]
    if entity_type == "alliance":
        info = resolve.alliances([entity_id]).get(entity_id)
    else:
        info = resolve.corporations([entity_id]).get(entity_id)
    if info is None:
        return None
    return entity_id, info.name, info.ticker


@login_required
@permission_required("fightlog.curate_fights")
def friendlies(request):
    form = FriendlyEntityForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("delete"):
            FriendlyEntity.objects.filter(pk=request.POST["delete"]).delete()
            messages.success(request, "Removed.")
            return redirect("fightlog:friendlies")
        if form.is_valid():
            found = _resolve_entity(form.cleaned_data["entity_type"], form.cleaned_data["identifier"])
            if found is None:
                messages.error(request, "Couldn't find that alliance or corporation.")
            else:
                entity_id, name, ticker = found
                FriendlyEntity.objects.update_or_create(
                    entity_type=form.cleaned_data["entity_type"],
                    entity_id=entity_id,
                    defaults={"name": name, "ticker": ticker},
                )
                messages.success(request, f"Added {name} [{ticker}] as an ally. Fights rebuild with it next time.")
                return redirect("fightlog:friendlies")
    return render(request, "fightlog/friendlies.html", {"form": form, "entities": FriendlyEntity.objects.all()})


@login_required
@permission_required("fightlog.basic_access")
def help_page(request):
    return render(request, "fightlog/help.html")
