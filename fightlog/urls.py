from django.urls import path

from .views import api, curation, fights, upload

app_name = "fightlog"

urlpatterns = [
    path("", fights.fight_list, name="index"),
    path("fight/<int:fight_id>/", fights.fight_detail, name="fight"),
    path("fight/<int:fight_id>/edit/<str:action>/", curation.edit, name="edit"),
    path("fight/<int:fight_id>/discord/", curation.discord_post, name="discord"),
    path("api/fight/<int:fight_id>/analysis/", api.analysis, name="api_analysis"),
    path("api/fight/<int:fight_id>/events/", api.events, name="api_events"),
    path("api/fight/<int:fight_id>/pilot/", api.pilot, name="api_pilot"),
    path("api/fight/<int:fight_id>/status/", api.fight_status, name="api_fight_status"),
    path("upload/", upload.upload_page, name="upload"),
    path("api/logs/", upload.upload_api, name="api_upload"),
    path("api/logs/<int:log_id>/", upload.log_status, name="api_log"),
    path("my-logs/", upload.my_logs, name="my_logs"),
    path("logs/<int:log_id>/delete/", upload.delete_log, name="delete_log"),
    path("friendlies/", curation.friendlies, name="friendlies"),
    path("help/", curation.help_page, name="help"),
]
