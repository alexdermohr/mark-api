#!/bin/sh
# Draft release boundary: a raw Compose start bypasses the operator control.
# Do not open, initialize, reconcile, or seal the durable recovery ledger yet.
set -eu
umask 077
printf '%s\n' 'mark-api: independent witness live deployment not attested' >&2
exit 1
# Future controlled startup only, after real UID, ledger-cutover, socket,
# network and paired-backup attestations have all passed.
exec /opt/mark-api/venv/bin/python -I -m mark_api.fence_witness "$@"
