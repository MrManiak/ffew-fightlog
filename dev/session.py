"""Print a login session key for a dev user (for scripted end-to-end tests only):
    docker compose exec auth python manage.py shell -c "exec(open('/home/allianceauth/session.py').read())" member
The user name is read from DEV_SESSION_USER (default: admin).
"""

import os
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore

from allianceauth.authentication.models import User

user = User.objects.get(username=os.environ.get("DEV_SESSION_USER", "admin"))
store = import_module(settings.SESSION_ENGINE).SessionStore() if settings.SESSION_ENGINE else SessionStore()
store[SESSION_KEY] = str(user.pk)
store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
store[HASH_SESSION_KEY] = user.get_session_auth_hash()
store.save()
print(f"SESSION {store.session_key}")
