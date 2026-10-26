# flake8: noqa

# Every setting in base.py can be overloaded by redefining it here.
import os

from .base import *

# These are required for Django to function properly. Don't touch.
ROOT_URLCONF = "testauth.urls"
WSGI_APPLICATION = "testauth.wsgi.application"
SECRET_KEY = "t$@h+j#yqhmuy$x7$fkhytd&drajgfsb-6+j9pqn*vj0)gq&-2"

# This is where css/images will be placed for your webserver to read
STATIC_ROOT = "/var/www/testauth/static/"

SITE_NAME = "testauth"
SITE_URL = "https://www.example.com"
CSRF_TRUSTED_ORIGINS = [SITE_URL]
DEBUG = False

INSTALLED_APPS += ["fightlog"]

ESI_SSO_CLIENT_ID = "dummy"
ESI_SSO_CLIENT_SECRET = "dummy"
ESI_SSO_CALLBACK_URL = "http://localhost:8000"
ESI_USER_CONTACT_EMAIL = "info@example.com"

REGISTRATION_VERIFY_EMAIL = False
EMAIL_HOST = ""
EMAIL_PORT = 587
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = ""

#######################################
# Add any custom settings below here. #
#######################################

# workarounds to suppress warnings
LOGGING = None
STATICFILES_DIRS = []
ANALYTICS_DISABLED = True

# Workaround for fixing the "Missing staticfiles manifest entry" bug in tests
STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.StaticFilesStorage"

# Alliance Auth needs Redis at startup. Locally: docker run -d -p 6379:6379 redis:8
REDIS_URL = os.environ.get("FIGHTLOG_TEST_REDIS_URL", "redis://127.0.0.1:6379")
CACHES["default"]["LOCATION"] = f"{REDIS_URL}/1"
BROKER_URL = f"{REDIS_URL}/0"

# Tasks run inline (AA's settings use Celery's old-style names, so this one does too).
CELERY_ALWAYS_EAGER = True
CELERY_EAGER_PROPAGATES_EXCEPTIONS = True

FIGHTLOG_ZKB_REQUEST_DELAY_MS = 100
