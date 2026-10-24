#!/bin/sh
# Dedicated UID50046 SQLite main/WAL/SHM owner. DRAFT HARD STOP.
# Direct docker compose --no-deps must not bypass the host cutover,
# historical-FD/MAP_SHARED, paired Witness backup or independent review gates.
set -eu
umask 077
[ "$#" -eq 0 ] || {
    printf '%s\n' 'mark-api: unexpected storage owner arguments' >&2
    exit 64
}
printf '%s\n' 'mark-api: independent SQLite owner live deployment not attested' >&2
exit 1
# Unreachable until a separately reviewed and root-attested release.
# Do not initialize a missing database, replace stale sockets or migrate it.
exec /opt/mark-api/venv/bin/python -I -m mark_api.storage_owner \
    --db /var/lib/mark-api/mark.sqlite \
    --socket /run/mark-api-storage/storage.sock \
    --runtime-uid 50044 --runtime-gid 50044
