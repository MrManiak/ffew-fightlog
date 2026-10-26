"""Seed the dev Auth: run with
    docker compose exec auth python manage.py shell -c "exec(open('/home/allianceauth/seed.py').read())"

Creates:
- admin (superuser, password DEV_ADMIN_PASSWORD) owning the characters in DEV_CHARACTER_IDS
- member (basic_access only) and curator (every fightlog permission)
- a Discord webhook pointing at a local mock receiver (DEV_DISCORD_WEBHOOK)
"""

import os

from django.contrib.auth.models import Permission

from allianceauth.authentication.models import CharacterOwnership, User, UserProfile
from allianceauth.eveonline.models import EveCharacter

from fightlog.models import DiscordWebhook

CHARACTER_IDS = [int(x) for x in os.environ.get("DEV_CHARACTER_IDS", "").split(",") if x.strip()]
PERMS = ["basic_access", "upload_any_log", "curate_fights", "post_to_discord"]


def perms(*names):
    return list(Permission.objects.filter(content_type__app_label="fightlog", codename__in=names))


def character(character_id):
    existing = EveCharacter.objects.filter(character_id=character_id).first()
    if existing:
        return existing
    try:
        return EveCharacter.objects.create_character(character_id)
    except Exception as ex:  # noqa: BLE001 - ESI hiccups shouldn't stop the seed
        print(f"  could not fetch character {character_id}: {ex}")
        return None


def give(user, chars, main=None):
    for char in chars:
        if char is None:
            continue
        CharacterOwnership.objects.update_or_create(
            character=char, defaults={"user": user, "owner_hash": f"dev-owner-{char.character_id}"}
        )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.main_character = main or next(c for c in chars if c)
    profile.save()


def fake_character(character_id, name):
    char, _ = EveCharacter.objects.get_or_create(
        character_id=character_id,
        defaults={
            "character_name": name,
            "corporation_id": 840323545,
            "corporation_name": "The Forsakened Few",
            "corporation_ticker": "FFEW",
            "alliance_id": 99011699,
            "alliance_name": "We Forsakened Few",
            "alliance_ticker": "WEFEW",
        },
    )
    return char


admin, created = User.objects.get_or_create(username="admin", defaults={"is_staff": True, "is_superuser": True})
admin.is_staff = admin.is_superuser = True
admin.set_password(os.environ.get("DEV_ADMIN_PASSWORD", "fightlog-dev"))
admin.save()
chars = [character(cid) for cid in CHARACTER_IDS] or [fake_character(3900000000, "Dev Admin")]
print("admin characters:", [c.character_name for c in chars if c])
give(admin, chars)

member, _ = User.objects.get_or_create(username="member")
member.set_password(os.environ.get("DEV_ADMIN_PASSWORD", "fightlog-dev"))
member.save()
give(member, [fake_character(3900000001, "Dev Member")])
member.user_permissions.set(perms("basic_access"))

curator, _ = User.objects.get_or_create(username="curator")
curator.set_password(os.environ.get("DEV_ADMIN_PASSWORD", "fightlog-dev"))
curator.save()
give(curator, [fake_character(3900000002, "Dev Curator")])
curator.user_permissions.set(perms(*PERMS))

DiscordWebhook.objects.update_or_create(
    name="dev-mock",
    defaults={"url": os.environ.get("DEV_DISCORD_WEBHOOK", "http://host.docker.internal:8765/api/webhooks/1/dev")},
)
print("seeded: admin, member, curator; webhook dev-mock")
