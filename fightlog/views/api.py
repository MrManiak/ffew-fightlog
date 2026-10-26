from django.contrib.auth.decorators import login_required, permission_required
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET

from fightlog.models import Fight
from fightlog.parser import Kind
from fightlog.services import analysis as analysis_service


def _fight(fight_id: int) -> Fight:
    fight = Fight.objects.filter(pk=fight_id).first()
    if fight is None:
        raise Http404("No such fight")
    return fight


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


MAX_WINDOW_SECONDS = 86400


def _window(request) -> tuple[int, int] | None:
    """The ``from``/``to`` time window (EVE epoch seconds, inclusive), or None for the whole fight."""
    try:
        lo, hi = int(request.GET["from"]), int(request.GET["to"])
    except (KeyError, TypeError, ValueError):
        return None
    if lo < 0 or lo > hi or hi - lo > MAX_WINDOW_SECONDS:
        return None
    return lo, hi


@login_required
@permission_required("fightlog.basic_access")
@require_GET
def analysis(request, fight_id: int):
    fight = _fight(fight_id)
    logs = analysis_service.parse_logs_param(fight, request.GET.get("logs"))
    bucket = _int(request.GET.get("bucket"), 10, 5, 60)
    if bucket not in (5, 10, 30, 60):
        bucket = 10
    result = analysis_service.analysis(
        fight,
        logs,
        request.GET.get("perspective"),
        bucket,
        request.GET.get("non_ships") == "1",
        _window(request),
    )
    result["active_logs"] = sorted(logs)
    result["built"] = fight.is_built
    return JsonResponse(result)


@login_required
@permission_required("fightlog.basic_access")
@require_GET
def events(request, fight_id: int):
    fight = _fight(fight_id)
    logs = analysis_service.parse_logs_param(fight, request.GET.get("logs"))
    start = _int(request.GET.get("start"), 0, 0, 2**31 - 1)
    end = _int(request.GET.get("end"), 2**31 - 1, 0, 2**31 - 1)
    window = _window(request)
    if window:
        start, end = max(start, window[0]), min(end, window[1])
    kinds = set()
    for name in (request.GET.get("kinds") or "").split(","):
        name = name.strip().upper()
        if name in Kind.__members__:
            kinds.add(int(Kind[name]))
    result = analysis_service.events(
        fight,
        logs,
        start,
        end,
        request.GET.get("key") or None,
        kinds or None,
        _int(request.GET.get("limit"), 200, 1, 1000),
        _int(request.GET.get("offset"), 0, 0, 10**7),
    )
    return JsonResponse(result)


@login_required
@permission_required("fightlog.basic_access")
@require_GET
def pilot(request, fight_id: int):
    fight = _fight(fight_id)
    key = request.GET.get("key")
    if not key:
        raise Http404("key required")
    logs = analysis_service.parse_logs_param(fight, request.GET.get("logs"))
    return JsonResponse(
        analysis_service.pilot(
            fight, logs, key, request.GET.get("non_ships") == "1", request.GET.get("perspective"), _window(request)
        )
    )


@login_required
@permission_required("fightlog.basic_access")
@require_GET
def fight_status(request, fight_id: int):
    fight = _fight(fight_id)
    return JsonResponse(
        {"built": fight.is_built, "data_version": fight.data_version, "built_version": fight.built_version}
    )
