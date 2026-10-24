#!/bin/sh
# UID 50045: trusted minimal host-loopback TCP ingress; no CDP, DB or owner
# socket mounts, and no credentials. Untrusted frontend remains network:none.
# UID50045 still has the host network namespace, but no direct TCP CONNECT
# is permitted after mandatory Linux Landlock v4 bootstrap. The restriction
# is inherited by the Python HTTP relay and its children.
set -eu
umask 077
[ "$#" -eq 0 ] || { printf '%s\n' 'mark-api: unexpected ingress arguments' >&2; exit 64; }
# Direct Compose and --no-deps must never bypass release attestation.
printf '%s\n' 'mark-api: public dashboard ingress live deployment not attested' >&2
exit 1
exec /usr/bin/python3 -I -S /etc/mark-api/ingress-landlock.py