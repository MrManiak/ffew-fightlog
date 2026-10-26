from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from fightlog import app_settings
from fightlog.models import CombatLog, Fight
from fightlog.services import curation
from fightlog.services.ingest import accept_upload


@login_required
@permission_required("fightlog.basic_access")
def upload_page(request):
    return render(
        request,
        "fightlog/upload.html",
        {
            "default_days": app_settings.FIGHTLOG_UPLOAD_DEFAULT_DAYS,
            "max_mb": app_settings.FIGHTLOG_MAX_UPLOAD_MB,
            "can_upload_any": request.user.has_perm("fightlog.upload_any_log"),
        },
    )


@login_required
@permission_required("fightlog.basic_access")
@require_POST
def upload_api(request):
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"status": "rejected", "message": "No file."}, status=400)
    if upload.size > app_settings.FIGHTLOG_MAX_UPLOAD_MB * 1024 * 1024:
        return JsonResponse(
            {"status": "rejected", "message": f"File is larger than {app_settings.FIGHTLOG_MAX_UPLOAD_MB} MB."},
            status=413,
        )
    result = accept_upload(request.user, upload.name, upload.read())
    payload = {"status": result.status, "message": result.message, "log_id": result.log.pk if result.log else None}
    return JsonResponse(payload, status=200 if result.ok else 422)


def _log_payload(log: CombatLog) -> dict:
    fights = Fight.objects.filter(engagements__log=log).distinct()
    return {
        "id": log.pk,
        "status": log.status,
        "status_label": log.get_status_display(),
        "message": log.status_message,
        "listener": log.listener_name,
        "fights": [{"id": f.pk, "title": f.title, "url": f.get_absolute_url(), "built": f.is_built} for f in fights],
    }


@login_required
@permission_required("fightlog.basic_access")
@require_http_methods(["GET", "DELETE"])
def log_status(request, log_id: int):
    log = CombatLog.objects.defer("raw_gz").filter(pk=log_id).first()
    if log is None:
        raise Http404("No such log")
    if request.method == "DELETE":
        if not curation.can_delete_log(request.user, log):
            return JsonResponse({"status": "forbidden"}, status=403)
        curation.delete_log(log, request.user)
        return JsonResponse({"status": "deleted"})
    return JsonResponse(_log_payload(log))


def _my_logs(user):
    owned = list(user.character_ownerships.values_list("character__character_id", flat=True))
    return (
        CombatLog.objects.defer("raw_gz")
        .filter(Q(uploads__user=user) | Q(listener_character_id__in=owned))
        .distinct()
        .order_by("-session_started_at", "-pk")
    )


@login_required
@permission_required("fightlog.basic_access")
def my_logs(request):
    logs = list(_my_logs(request.user)[:500])
    for log in logs:
        log.fight_list = list(Fight.objects.filter(engagements__log=log).distinct())
    return render(request, "fightlog/my_logs.html", {"logs": logs})


@login_required
@permission_required("fightlog.basic_access")
@require_POST
def delete_log(request, log_id: int):
    log = get_object_or_404(CombatLog.objects.defer("raw_gz"), pk=log_id)
    if not curation.can_delete_log(request.user, log):
        messages.error(request, "You can only delete logs you uploaded or logs of your own characters.")
    else:
        curation.delete_log(log, request.user)
        messages.success(request, f"Deleted the log of {log.listener_name}.")
    target = request.POST.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect("fightlog:my_logs")
