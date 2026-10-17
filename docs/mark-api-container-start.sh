#!/bin/sh
# Docker-installed product path. Only the existing DB may be opened.
set -eu
umask 077
[ "$#" -eq 0 ] || { printf '%s\n' 'mark-api: unexpected launcher arguments' >&2; exit 64; }
/bin/sh /etc/mark-api/bootstrap.sh \
    --db /var/lib/mark-api/mark.sqlite \
    --backup-dir /var/lib/mark-api-backups
# No write-disable flags: mark-api-launch intentionally composes Writes default-on.
exec /opt/mark-api/venv/bin/python -I -m mark_api.launcher \
    --db /var/lib/mark-api/mark.sqlite \
    --cdp-port 9222 --dashboard-port 8875 --write-port 8876 > /run/mark-api/launcher.log