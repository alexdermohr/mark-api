# Build only from a verified, committed mark-api wheel. The build helper
# creates a minimal context: wheel plus the reviewed runtime scripts.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ARG DEBIAN_FRONTEND=noninteractive
ARG MARK_UID=50042
ARG MARK_GID=50042
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1

RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends \
       python3-minimal python3.12 python3.12-venv ca-certificates \
       findutils coreutils passwd \
    && rm -rf /var/lib/apt/lists/*

# This venv must be root-managed. Never chown an existing live installation.
RUN /usr/bin/python3.12 -m venv /opt/mark-api/venv
COPY mark_api-*.whl /opt/mark-api-wheels/
RUN set -eu; \
    set -- /opt/mark-api-wheels/*.whl; \
    [ "$#" -eq 1 ]; \
    /opt/mark-api/venv/bin/python -m pip install --no-index --no-deps "$1"; \
    /opt/mark-api/venv/bin/python -m pip install --no-cache-dir \
       --only-binary=:all: --no-deps Pillow==12.3.0 websocket-client==1.9.2; \
    rm -rf /opt/mark-api-wheels

RUN set -eu; \
    case "$MARK_UID:$MARK_GID" in *[!0-9:]*|"") exit 1;; esac; \
    [ "$MARK_UID" -ge 10000 ] && [ "$MARK_UID" -lt 61184 ]; \
    [ "$MARK_GID" -ge 10000 ] && [ "$MARK_GID" -lt 61184 ]; \
    ! getent passwd "$MARK_UID"; ! getent group "$MARK_GID"; \
    groupadd --gid "$MARK_GID" mark-api; \
    useradd --uid "$MARK_UID" --gid "$MARK_GID" --system \
      --home-dir /nonexistent --shell /usr/sbin/nologin \
      --no-create-home mark-api; \
    ! getent passwd 50044; ! getent group 50044; \
    groupadd --gid 50044 mark-api-owner; \
    useradd --uid 50044 --gid 50044 --system \
      --home-dir /var/lib/mark-api-media --shell /usr/sbin/nologin \
      --no-create-home mark-api-owner; \
    ! getent passwd 50045; ! getent group 50045; \
    groupadd --gid 50045 mark-api-ingress; \
    useradd --uid 50045 --gid 50045 --system \
      --home-dir /nonexistent --shell /usr/sbin/nologin \
      --no-create-home mark-api-ingress; \
    ! getent passwd 50046; ! getent group 50046; \
    groupadd --gid 50046 mark-api-storage; \
    useradd --uid 50046 --gid 50046 --system \
      --home-dir /var/lib/mark-api --shell /usr/sbin/nologin \
      --no-create-home mark-api-storage; \
    install -d -o root -g root -m 0755 /etc/mark-api; \
    install -d -o mark-api-storage -g mark-api-storage -m 0700 \
      /var/lib/mark-api /var/lib/mark-api-backups; \
    install -d -o mark-api-owner -g mark-api-owner -m 0700 \
      /var/lib/mark-api-media; \
    printf '%s\\n' 'UNATTESTED DRAFT: direct Python entrypoints disabled' \
      > /etc/mark-api/UNATTESTED_DRAFT; \
    chmod 0444 /etc/mark-api/UNATTESTED_DRAFT

COPY --chmod=0644 mark-api-bootstrap.sh /etc/mark-api/bootstrap.sh
COPY --chmod=0644 mark-api-preflight.py /etc/mark-api/preflight.py
COPY --chmod=0644 mark-api-container-healthcheck.py /etc/mark-api/healthcheck.py
COPY --chmod=0644 mark-api-container-start.sh /etc/mark-api/start.sh
COPY --chmod=0644 mark-api-container-frontend.sh /etc/mark-api/frontend.sh
COPY --chmod=0644 mark-api-container-ingress.sh /etc/mark-api/ingress.sh
COPY --chmod=0644 mark-api-container-ingress-landlock.py /etc/mark-api/ingress-landlock.py
COPY --chmod=0644 mark-api-container-witness.sh /etc/mark-api/witness.sh
COPY --chmod=0644 mark-api-container-storage.sh /etc/mark-api/storage.sh
COPY --chmod=0644 mark-api-container-backup.sh /etc/mark-api/backup.sh

# Verify actual distro stdlib before any Mark application import.
RUN /bin/sh /etc/mark-api/bootstrap.sh --help
USER mark-api:mark-api
WORKDIR /
# Unqualified docker run and stale Compose must never start the owner.
# Only reviewed Compose may explicitly select /etc/mark-api/start.sh.
ENTRYPOINT ["/usr/bin/false"]
