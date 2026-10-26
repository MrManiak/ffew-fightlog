from django.core.management.base import BaseCommand

from fightlog.models import Fight


class Command(BaseCommand):
    help = "Rebuild fights (all of them, or the given IDs)."

    def add_arguments(self, parser):
        parser.add_argument("fight_ids", nargs="*", type=int)
        parser.add_argument("--sync", action="store_true", help="Run here instead of queueing Celery tasks.")

    def handle(self, *args, **options):
        from fightlog.services.fights import bump_versions, queue_rebuilds
        from fightlog.services.rebuild import rebuild_fight

        ids = options["fight_ids"] or list(Fight.objects.values_list("pk", flat=True))
        bump_versions(ids)
        if options["sync"]:
            for fight_id in ids:
                rebuild_fight(fight_id)
            self.stdout.write(self.style.SUCCESS(f"Rebuilt {len(ids)} fights."))
            return
        queue_rebuilds(ids, countdown=1)
        self.stdout.write(self.style.SUCCESS(f"Queued {len(ids)} rebuilds."))
