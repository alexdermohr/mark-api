# Build only from a verified, committed mark-api wheel. The build helper
# creates a minimal context: wheel plus the three reviewed runtime scripts.
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
      --home-dir /var/lib/mark-api --shell /usr/sbin/nologin \
      --no-create-home mark-api; \
    install -d -o root -g root -m 0755 /etc/mark-api; \
    install -d -o mark-api -g mark-api -m 0700 \
      /var/lib/mark-api /var/lib/mark-api-backups

COPY --chmod=0644 mark-api-bootstrap.sh /etc/mark-api/bootstrap.sh
COPY --chmod=0644 mark-api-preflight.py /etc/mark-api/preflight.py
COPY --chmod=0644 mark-api-container-start.sh /etc/mark-api/start.sh
COPY --chmod=0644 mark-api-container-backup.sh /etc/mark-api/backup.sh

# Verify actual distro stdlib before any Mark application import.
RUN /bin/sh /etc/mark-api/bootstrap.sh --help
USER mark-api:mark-api
WORKDIR /var/lib/mark-api
ENTRYPOINT ["/bin/sh", "/etc/mark-api/start.sh"]
