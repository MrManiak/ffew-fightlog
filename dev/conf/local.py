# Development settings for the local Alliance Auth stack (dev/docker-compose.yml).
from celery.schedules import crontab

from .base import *

SECRET_KEY = os.environ.get("AA_SECRET_KEY")
SITE_NAME = os.environ.get("AA_SITENAME", "Fightlog Dev")
SITE_URL = os.environ.get("AA_SITE_URL", "http://localhost:8000")
CSRF_TRUSTED_ORIGINS = [SITE_URL, "http://127.0.0.1:8000"]
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "auth", "host.docker.internal"]
DEBUG = True
# Dev runs over plain HTTP: AA's base settings mark these cookies Secure.
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
DATABASES["default"] = {
    "ENGINE": "django.db.backends.mysql",
    "NAME": os.environ.get("AA_DB_NAME"),
    "USER": os.environ.get("AA_DB_USER"),
    "PASSWORD": os.environ.get("AA_DB_PASSWORD"),
    "HOST": os.environ.get("AA_DB_HOST"),
    "PORT": os.environ.get("AA_DB_PORT", "3306"),
    "OPTIONS": {"charset": os.environ.get("AA_DB_CHARSET", "utf8mb4")},
}

# SSO isn't configured in dev: sign in through /admin/ with the seeded superuser.
ESI_SSO_CALLBACK_URL = f"{SITE_URL}/sso/callback"
ESI_SSO_CLIENT_ID = os.environ.get("ESI_SSO_CLIENT_ID", "dummy")
ESI_SSO_CLIENT_SECRET = os.environ.get("ESI_SSO_CLIENT_SECRET", "dummy")
ESI_USER_CONTACT_EMAIL = os.environ.get("ESI_USER_CONTACT_EMAIL", "noreply@example.com")

REGISTRATION_VERIFY_EMAIL = False
EMAIL_HOST = ""
DEFAULT_FROM_EMAIL = ""

ROOT_URLCONF = "myauth.urls"
WSGI_APPLICATION = "myauth.wsgi.application"
STATIC_ROOT = "/var/www/myauth/static/"
BROKER_URL = f"redis://{os.environ.get('AA_REDIS', 'redis:6379')}/0"
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://{os.environ.get('AA_REDIS', 'redis:6379')}/1",
    }
}

INSTALLED_APPS += [
    "fightlog",
]

# ffew-fightlog: daily purge of hidden no-PvP logs (docs/install.md).
CELERYBEAT_SCHEDULE["fightlog_purge_expired_logs"] = {
    "task": "fightlog.tasks.purge_expired_logs",
    "schedule": crontab(minute=17, hour=4),
    "apply_offset": True,
}
