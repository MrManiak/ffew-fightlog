# Development image: the official Alliance Auth image plus ffew-fightlog's dependencies.
# The plugin source itself is mounted at /opt/ffew-fightlog and found via PYTHONPATH,
# so code changes need no rebuild.
ARG AA_DOCKER_TAG=registry.gitlab.com/allianceauth/allianceauth/auth:v5.5.0
FROM ${AA_DOCKER_TAG}

USER root
RUN pip install --no-cache-dir "allianceauth-app-utils>=1.33" "django-esi>=10,<11" requests
USER ${AUTH_USER}

ENV PYTHONPATH=/opt/ffew-fightlog
