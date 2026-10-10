#!/bin/sh
# Operator entrypoint: never send a mutable Docker tag to Compose.
# This does not replace independent UID/volume/mmap/CDP operational attestation.
set -eu
usage() {
    printf '%s\n' 'usage: sh mark-api-container-control.sh verify|live|backup sha256:<64-lowercase-hex> EXACT_COMMIT_SHA [BACKUP_ID]' >&2
    exit 64
}
invalid_image() {
    printf '%s\n' 'mark-api: immutable image ID sha256:<64-lowercase-hex> required' >&2
    exit 64
}
fail() {
    printf 'mark-api: %s\n' "$1" >&2
    exit 1
}
[ "$#" -ge 3 ] || usage
action=$1
image=$2
expected=$3
shift 3
case "$action" in verify|live|backup) ;; *) usage ;; esac
case "$image" in sha256:*) digest=${image#sha256:} ;; *) invalid_image ;; esac
case "$digest" in ''|*[!0-9a-f]*) invalid_image ;; esac
[ "${#digest}" -eq 64 ] || invalid_image
case "$expected" in ''|*[!0-9a-f]*) usage ;; esac
[ "${#expected}" -eq 40 ] || usage
case "$action" in
    backup)
        [ "$#" -eq 1 ] || usage
        instance=$1
        case "$instance" in ''|*[!A-Za-z0-9_-]*) usage ;; esac
        [ "${#instance}" -le 48 ] || usage
        ;;
    *) [ "$#" -eq 0 ] || usage ;;
esac
[ -z "${DOCKER_HOST-}" ] && [ -z "${DOCKER_CONTEXT-}" ] || fail 'unexpected Docker endpoint'
# All Git comparisons use the actual commit object; never a replacement tree.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
unset GIT_ALTERNATE_OBJECT_DIRECTORIES
export GIT_NO_REPLACE_OBJECTS=1
export GIT_GRAFT_FILE=/dev/null
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
actual=$(git -C "$repo" rev-parse --verify HEAD) || fail 'HEAD unavailable'
[ "$actual" = "$expected" ] || fail 'HEAD/revision drift'
[ -z "$(git -C "$repo" status --porcelain --untracked-files=normal)" ] ||
    fail 'dirty release checkout'
resolved=$(git -C "$repo" rev-parse --verify "$expected^{commit}") ||
    fail 'unknown release commit'
[ "$resolved" = "$expected" ] || fail 'invalid release commit'
# Fail closed if caller supplies an image ID not present on the *local* daemon.
docker=/usr/bin/docker
format_id='{{.Id}}'
format_rev='{{index .Config.Labels "org.opencontainers.image.revision"}}'
actual_image=$("$docker" --host unix:///var/run/docker.sock image inspect --format "$format_id" "$image") ||
    fail 'immutable image not present locally'
[ "$actual_image" = "$image" ] || fail 'image identity mismatch'
actual_rev=$("$docker" --host unix:///var/run/docker.sock image inspect --format "$format_rev" "$image") ||
    fail 'image revision label missing'
[ "$actual_rev" = "$expected" ] || fail 'image revision label does not match commit'
actual_user=$("$docker" --host unix:///var/run/docker.sock image inspect --format '{{.Config.User}}' "$image") ||
    fail 'image user unavailable'
[ "$actual_user" = 'mark-api:mark-api' ] || fail 'image is not configured for Mark service user'
# Compose receives exclusively the checked immutable ID, never a caller's tag.
MARK_API_IMAGE_DIGEST="$digest"
export MARK_API_IMAGE_DIGEST
compose="$repo/docs/mark-api-container.compose.yaml"
"$docker" --host unix:///var/run/docker.sock compose -f "$compose" --profile live --profile backup config --quiet ||
    fail 'Compose configuration invalid'
case "$action" in
    verify)
        printf '%s\n' "mark-api: immutable image and revision verified"
        ;;
    live|backup)
        # These checks are necessary, but not sufficient: the operator must
        # independently attest existing storage inodes, mappings and CDP rights.
        passwd_name=$(/usr/bin/getent passwd 50042 | /usr/bin/cut -d: -f1)
        group_name=$(/usr/bin/getent group 50042 | /usr/bin/cut -d: -f1)
        [ "$passwd_name" = mark-api ] && [ "$group_name" = mark-api ] ||
            fail 'exclusive host service UID/GID 50042 not reserved'
        for name in mark-api-data-v1 mark-api-backups-v1; do
            found=$("$docker" --host unix:///var/run/docker.sock volume inspect --format '{{.Name}}' "$name") ||
                fail 'required pre-attested external volume missing'
            [ "$found" = "$name" ] || fail 'volume identity changed'
        done
        if [ "$action" = live ]; then
            exec "$docker" --host unix:///var/run/docker.sock compose -f "$compose" --profile live \
                up --no-deps --no-build --pull never --detach mark-api
        fi
        exec "$docker" --host unix:///var/run/docker.sock compose -f "$compose" --profile backup \
            run --rm --no-deps --pull never mark-api-backup "$instance"
        ;;
esac