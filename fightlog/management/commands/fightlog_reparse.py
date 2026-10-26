from django.core.management.base import BaseCommand

from fightlog.models import CombatLog
from fightlog.parser import PARSER_VERSION


class Command(BaseCommand):
    help = "Re-parse logs written by an older parser version (or all logs with --all)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Re-parse every log, not just stale ones.")
        parser.add_argument("--sync", action="store_true", help="Run here instead of queueing Celery tasks.")

    def handle(self, *args, **options):
        from fightlog.services import ingest
        from fightlog.services.rebuild import rebuild_fight
        from fightlog.tasks import process_log

        logs = CombatLog.objects.exclude(status=CombatLog.Status.REJECTED)
        if not options["all"]:
            logs = logs.filter(parser_version__lt=PARSER_VERSION)
        ids = list(logs.values_list("pk", flat=True))
        if options["sync"]:
            affected = set()
            for log in CombatLog.objects.filter(pk__in=ids):
                affected |= ingest.process_log(log)
            for fight_id in sorted(affected):
                rebuild_fight(fight_id)
            self.stdout.write(self.style.SUCCESS(f"Re-parsed {len(ids)} logs, rebuilt {len(affected)} fights."))
            return
        # process_log queues the rebuilds of the fights each log touches.
        for log_id in ids:
            process_log.apply_async(args=[log_id], priority=8)
        self.stdout.write(self.style.SUCCESS(f"Queued {len(ids)} logs for re-parsing."))
