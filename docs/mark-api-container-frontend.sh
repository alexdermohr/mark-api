#!/bin/sh
# UID 50042: untrusted HTTP dashboard transport only.
# No Chrome/CDP arguments, SQLite paths, witness authority or media handles.
set -eu
umask 077
[ "$#" -eq 0 ] || { printf '%s\n' 'mark-api: unexpected frontend arguments' >&2; exit 64; }
# Draft hard stop: --no-deps bypasses Compose service_healthy dependencies.
# Never publish the dashboard listener, even with an old executor socket.
printf '%s\n' 'mark-api: independent frontend live deployment not attested' >&2
exit 1
exec /opt/mark-api/venv/bin/python -I -m mark_api.dashboard_relay \
    netless-frontend \
    --backend-socket /run/mark-api-executor/dashboard.sock \
    --expected-owner-uid 50044 \
    --ingress-socket /run/mark-api-ingress/frontend.sock \
    --ingress-uid 50045 --ingress-gid 50045