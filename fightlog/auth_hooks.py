from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


class FightlogMenuItem(MenuItemHook):
    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Fight Logs"),
            "fas fa-crosshairs fa-fw",
            "fightlog:index",
            order=1100,
            navactive=["fightlog:"],
        )

    def render(self, request):
        if request.user.has_perm("fightlog.basic_access"):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return FightlogMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "fightlog", r"^fightlog/")
