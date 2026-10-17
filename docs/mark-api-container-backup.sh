#!/bin/sh
# An individual, create-only backup invocation. No implicit retry.
set -eu
umask 077
[ "$#" -eq 1 ] || { printf '%s\n' 'mark-api: one backup instance ID required' >&2; exit 64; }
instance=$1
case "$instance" in
    ''|*[!A-Za-z0-9_-]*)
        printf '%s\n' 'mark-api: invalid backup instance ID' >&2
        exit 64 ;;
esac
[ "${#instance}" -le 48 ] || exit 64
/bin/sh /etc/mark-api/bootstrap.sh \
    --db /var/lib/mark-api/mark.sqlite \
    --backup-dir /var/lib/mark-api-backups
exec /opt/mark-api/venv/bin/python -I -m mark_api.backup_cli \
    --db /var/lib/mark-api/mark.sqlite \
    --backup "/var/lib/mark-api-backups/mark-${instance}.sqlite"
