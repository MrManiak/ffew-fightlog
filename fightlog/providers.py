"""ESI access through django-esi's OpenAPI client, following AA core's pattern."""

from esi.openapi_clients import ESIClientProvider

from . import __title__, __url__, __version__

ESI_COMPATIBILITY_DATE = "2026-05-19"  # matches allianceauth.eveonline; re-check on AA upgrades

ESI_OPERATIONS = [
    "PostUniverseIds",
    "PostUniverseNames",
    "PostCharactersAffiliation",
    "GetCorporationsCorporationId",
    "GetAlliancesAllianceId",
    "GetUniverseSystemsSystemId",
    "GetUniverseConstellationsConstellationId",
    "GetUniverseRegionsRegionId",
]

esi = ESIClientProvider(
    ua_appname=__title__,
    ua_version=__version__,
    ua_url=__url__,
    compatibility_date=ESI_COMPATIBILITY_DATE,
    operations=ESI_OPERATIONS,
)
