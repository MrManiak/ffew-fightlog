from django.apps import AppConfig

from fightlog import __version__


class FightlogConfig(AppConfig):
    name = "fightlog"
    label = "fightlog"
    verbose_name = f"Fightlog v{__version__}"
    default_auto_field = "django.db.models.BigAutoField"
