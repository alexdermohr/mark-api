#!/bin/sh
# Docker-installed product path. Only the existing DB may be opened.
set -eu
umask 077
[ "$#" -eq 0 ] || { printf '%s\n' 'mark-api: unexpected launcher arguments' >&2; exit 64; }
# Draft hard stop: direct Compose bypasses the operator Control guard.
printf '%s\n' 'mark-api: trusted browser executor live deployment not attested' >&2
exit 1
/bin/sh /etc/mark-api/bootstrap.sh \
    --db /var/lib/mark-api/mark.sqlite \
    --backup-dir /var/lib/mark-api-backups \
    --service-account mark-api-owner
# No write-disable flags: mark-api-launch intentionally composes Writes default-on.
exec /opt/mark-api/venv/bin/python -I -m mark_api.launcher \
    --db /var/lib/mark-api/mark.sqlite \
    --cdp-port 9222 --dashboard-port 18875 --write-port 18876 \
    --witness-socket /run/mark-api-witness/runtime.sock \
    --witness-owner-uid 50043 \
    --frontend-socket /run/mark-api-executor/dashboard.sock \
    --frontend-uid 50042 --frontend-gid 50042 > /run/mark-api/launcher.log