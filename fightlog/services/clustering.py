"""Engagement → fight assignment and automatic merging (technical plan §4.4)."""

from datetime import timedelta

from django.db import transaction

from fightlog import app_settings
from fightlog.locks import cache_lock
from fightlog.models import CombatLog, Engagement, Fight

from .fights import bump_versions


def _fight_keys(fight: Fight) -> set[str]:
    keys: set[str] = set()
    for actor_keys in fight.engagements.values_list("actor_keys", flat=True):
        keys.update(actor_keys or [])
    return keys


def _overlap(fight: Fight, engagement) -> float:
    """Seconds a fight and an engagement share (negative when they don't touch)."""
    lo = max(fight.start_at, engagement.start_at)
    hi = min(fight.end_at, engagement.end_at)
    return (hi - lo).total_seconds()


def assign_log_engagements(log: CombatLog) -> set[int]:
    """Put each unassigned engagement of a log into a fight. Returns affected fight IDs."""
    tolerance = timedelta(seconds=app_settings.FIGHTLOG_MERGE_TOLERANCE_SECONDS)
    affected: set[int] = set()
    with cache_lock("clustering", timeout=300):
        with transaction.atomic():
            for engagement in log.engagements.filter(fight__isnull=True):
                keys = set(engagement.actor_keys or [])
                listener_key = f"p:{log.listener_name}"
                candidates = Fight.objects.filter(
                    start_at__lte=engagement.end_at + tolerance, end_at__gte=engagement.start_at - tolerance
                )
                qualifying = []
                for fight in candidates:
                    fight_keys = _fight_keys(fight)
                    shared = len(keys & fight_keys)
                    if shared or listener_key in fight_keys:
                        qualifying.append((shared, fight))
                if not qualifying and not keys - {listener_key}:
                    # An observer log (only third-party tackle, no named pilots): join the
                    # overlapping fight it shares the most time with.
                    nearby = list(candidates)  # already within the merge tolerance
                    if nearby:
                        qualifying = [(0, max(nearby, key=lambda f, e=engagement: _overlap(f, e)))]
                if not qualifying:
                    fight = Fight.objects.create(
                        title=f"Fight {engagement.start_at:%Y-%m-%d %H:%M}",
                        start_at=engagement.start_at,
                        end_at=engagement.end_at,
                    )
                elif len(qualifying) == 1:
                    fight = qualifying[0][1]
                else:
                    curated = [(s, f) for s, f in qualifying if f.is_curated]
                    if curated:
                        fight = max(curated, key=lambda sf: (sf[0], -sf[1].pk))[1]
                    else:
                        from .curation import merge_fights

                        fights = [f for _, f in qualifying]
                        fight = merge_fights(fights, user=None)
                engagement.fight = fight
                engagement.save(update_fields=["fight"])
                changed = False
                if engagement.start_at < fight.start_at:
                    fight.start_at = engagement.start_at
                    changed = True
                if engagement.end_at > fight.end_at:
                    fight.end_at = engagement.end_at
                    changed = True
                if changed:
                    fight.save(update_fields=["start_at", "end_at"])
                affected.add(fight.pk)
            bump_versions(affected)
    return affected


def refresh_window(fight: Fight) -> None:
    span = fight.engagements.order_by("start_at").values_list("start_at", "end_at")
    rows = list(span)
    if rows:
        fight.start_at = min(r[0] for r in rows)
        fight.end_at = max(r[1] for r in rows)
        fight.save(update_fields=["start_at", "end_at"])


__all__ = ["Engagement", "assign_log_engagements", "refresh_window"]
