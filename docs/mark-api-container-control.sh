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

# Run every Git check with a minimal fixed environment and override dangerous
# executable local configuration (notably core.fsmonitor). Caller-provided
# GIT_CONFIG_COUNT/PARAMETERS, config include paths and Git helpers are inert.
safe_git() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        GIT_GRAFT_FILE=/dev/null /usr/bin/git \
        -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -c diff.external= -c core.pager=cat "$@"
}

# Check checkout-local Git configuration *before* any status/archive operation.
# Untrusted filter drivers, fsmonitor, includes, worktreeConfig and unknown
# repository options are not permitted in a release-attested checkout.
require_inert_git_config() {
    keys=$(safe_git -C "$repo" config --local --list --name-only --includes) || {
        printf '%s\n' 'mark-api: cannot inspect local Git configuration' >&2
        return 1
    }
    unexpected=$(printf '%s\n' "$keys" |
        /usr/bin/grep -Ev '^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|symlinks)|remote\.[^.]+\.(url|fetch)|branch\..+\.(remote|merge)|user\.(name|email)|init\.defaultbranch|pull\.rebase|push\.default|gc\.auto|safe\.directory)$' || :)
    if [ -n "$unexpected" ]; then
        printf '%s\n' 'mark-api: untrusted or unknown local Git configuration' >&2
        return 1
    fi
}
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
require_inert_git_config || exit 1
actual=$(safe_git -C "$repo" rev-parse --verify HEAD) || fail 'HEAD unavailable'
[ "$actual" = "$expected" ] || fail 'HEAD/revision drift'
# Never execute a worktree status check under mutable .git/config. A private
# Git metadata snapshot binds HEAD and index to an inert synthetic config.
common=$(safe_git -C "$repo" rev-parse --path-format=absolute --git-common-dir) ||
    fail 'Git common directory unavailable'
gitdir=$(safe_git -C "$repo" rev-parse --absolute-git-dir) ||
    fail 'Git checkout directory unavailable'
[ -d "$common/objects" ] && [ -f "$gitdir/index" ] ||
    fail 'Git objects or index unavailable'
umask 077
attest_dir=$(mktemp -d /tmp/mark-api-control.XXXXXXXX) ||
    fail 'Git snapshot staging unavailable'
trap 'rm -rf -- "$attest_dir"' 0
trap 'exit 1' 1 2 3 15
mkdir -m 0700 "$attest_dir/objects" "$attest_dir/refs" ||
    fail 'Git snapshot staging unavailable'
cp -- "$gitdir/index" "$attest_dir/index" ||
    fail 'Git index copy unavailable'
printf '%s\n' "$expected" > "$attest_dir/HEAD"
printf '[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = true\n' > "$attest_dir/config"
attested_git() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        GIT_GRAFT_FILE=/dev/null \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="$common/objects" \
        /usr/bin/git --git-dir="$attest_dir" --work-tree="$repo" \
        -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -c diff.external= -c core.pager=cat "$@"
}
status=$(attested_git status --porcelain --untracked-files=normal) ||
    fail 'Git snapshot status unavailable'
[ -z "$status" ] || fail 'dirty release checkout'
resolved=$(attested_git rev-parse --verify "$expected^{commit}") ||
    fail 'unknown release commit'
[ "$resolved" = "$expected" ] || fail 'invalid release commit'
rm -rf -- "$attest_dir"
trap - 0
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