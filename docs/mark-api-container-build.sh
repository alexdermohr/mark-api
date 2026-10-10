#!/bin/sh
# Build a container solely from one clean, exact Git commit.
set -eu
# Never resolve a reviewed SHA through replacement objects or legacy grafts.
# Local Git config inherited from caller must not redirect the object database.
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
[ "$#" -eq 1 ] || { printf '%s\n' 'usage: sh docs/mark-api-container-build.sh EXACT_HEAD_SHA' >&2; exit 64; }
expected=$1
case "$expected" in
    *[!0-9a-f]*|'') exit 64 ;;
esac
[ "${#expected}" -eq 40 ] || exit 64

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
head=$(safe_git -C "$repo" rev-parse --verify HEAD)
[ "$head" = "$expected" ] || { printf '%s\n' 'mark-api: HEAD changed' >&2; exit 1; }
# Pin executable Git configuration by using a private, minimal Git directory.
# Its HEAD is the attested commit and its index is regenerated from that
# commit's tree, never copied from the mutable checkout's index or config.
common=$(safe_git -C "$repo" rev-parse --path-format=absolute --git-common-dir) || exit 1
[ -d "$common/objects" ] || exit 1
umask 077
temp=$(mktemp -d /tmp/mark-api-image.XXXXXXXX) || exit 1
trap 'rm -rf -- "$temp"' 0
trap 'exit 1' 1 2 3 15
mkdir -m 0700 "$temp/git" "$temp/git/objects" "$temp/git/refs" \
    "$temp/source" "$temp/context" "$temp/docs"
printf '%s\n' "$expected" > "$temp/git/HEAD"
printf '[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = true\n' > "$temp/git/config"
attested_git() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        GIT_GRAFT_FILE=/dev/null \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="$common/objects" \
        /usr/bin/git --git-dir="$temp/git" --work-tree="$repo" \
        -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -c diff.external= -c core.pager=cat "$@"
}
attested_git read-tree "$expected" || exit 1
status=$(attested_git status --porcelain --untracked-files=normal) || exit 1
[ -z "$status" ] || {
    printf '%s\n' 'mark-api: source checkout is dirty' >&2
    exit 1
}
# The alternate object store is untrusted even when it names a reviewed OID.
# Capture and independently hash every reachable object of this one commit.
# The later archives may read only our freshly populated private object store.
fail() {
    printf 'mark-api: %s\n' "$1" >&2
    exit 1
}
private_git() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        GIT_GRAFT_FILE=/dev/null \
        /usr/bin/git --git-dir="$temp/git" --work-tree="$repo" \
        -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -c diff.external= -c core.pager=cat "$@"
}
attest_object() {
    object_type=$1
    object_id=$2
    case "$object_id" in ''|*[!0-9a-f]*) fail 'attested Git object content mismatch' ;; esac
    [ "${#object_id}" -eq 40 ] || fail 'attested Git object content mismatch'
    attested_git cat-file "$object_type" "$object_id" > "$temp/object.raw" ||
        fail 'attested Git object content mismatch'
    observed=$(private_git hash-object -t "$object_type" --stdin < "$temp/object.raw") ||
        fail 'attested Git object content mismatch'
    [ "$observed" = "$object_id" ] || fail 'attested Git object content mismatch'
    stored=$(private_git hash-object -w -t "$object_type" --stdin < "$temp/object.raw") ||
        fail 'attested Git object content mismatch'
    [ "$stored" = "$object_id" ] || fail 'attested Git object content mismatch'
}
attested_git rev-list --objects --no-walk "$expected" > "$temp/source-objects.list" ||
    fail 'attested Git object content mismatch'
while IFS= read -r object_line; do
    object_id=${object_line%% *}
    object_type=$(attested_git cat-file -t "$object_id") ||
        fail 'attested Git object content mismatch'
    case "$object_type" in commit|tree|blob) ;; *) fail 'attested Git object content mismatch' ;; esac
    attest_object "$object_type" "$object_id"
done < "$temp/source-objects.list"
# Re-traverse the verified copy: omissions in a mutable source object-list
# must not permit an incomplete release tree.
private_git rev-list --objects --no-walk "$expected" > /dev/null ||
    fail 'attested Git object content mismatch'
# Reject even inactive replacement/graft metadata: no ambiguous revision trust.
[ -z "$(safe_git -C "$repo" for-each-ref --format='%(refname)' refs/replace)" ] || {
    printf '%s\n' 'mark-api: Git replacement references are not allowed' >&2
    exit 1
}
[ ! -e "$common/info/grafts" ] && [ ! -L "$common/info/grafts" ] || {
    printf '%s\n' 'mark-api: Git grafts are not allowed' >&2
    exit 1
}
[ -z "${DOCKER_HOST-}" ] && [ -z "${DOCKER_CONTEXT-}" ] || {
    printf '%s\n' 'mark-api: remote/custom Docker targets require separate attestation' >&2
    exit 1
}
private_git archive "$expected" -- pyproject.toml README.md src > "$temp/source.tar" ||
    fail 'verified source archive unavailable'
/usr/bin/tar -xf "$temp/source.tar" -C "$temp/source" ||
    fail 'verified source extraction failed'
"${MARK_UV:-uv}" build --wheel --offline --out-dir "$temp/context" "$temp/source"
set -- "$temp/context"/*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || {
    printf '%s\n' 'mark-api: expected exactly one verified wheel' >&2
    exit 1
}
private_git archive "$expected" -- \
    docs/mark-api-container.Dockerfile \
    docs/mark-api-container-start.sh \
    docs/mark-api-container-backup.sh \
    docs/mark-api-bootstrap.sh docs/mark-api-preflight.py > "$temp/docs.tar" ||
    fail 'verified Docker files unavailable'
/usr/bin/tar -xf "$temp/docs.tar" -C "$temp" ||
    fail 'verified Docker files extraction failed'
for file in mark-api-container.Dockerfile mark-api-container-start.sh \
            mark-api-container-backup.sh mark-api-bootstrap.sh \
            mark-api-preflight.py; do
    cp -- "$temp/docs/$file" "$temp/context/$file"
done
# Image/build plugin lookup must not inherit mutable caller CLI configuration.
# Only the trusted system Docker CLI plugin locations are usable.
mkdir -m 0700 "$temp/docker-config" ||
    fail 'private Docker configuration unavailable'
docker_cli() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent DOCKER_CONFIG="$temp/docker-config" \
        /usr/bin/docker --config "$temp/docker-config" \
        --host unix:///var/run/docker.sock "$@"
}
short=$(printf '%.12s' "$expected")
tag="mark-api:pr75-$short"
docker_cli build --pull=false \
    --label "org.opencontainers.image.revision=$expected" \
    --file "$temp/context/mark-api-container.Dockerfile" --tag "$tag" "$temp/context"
docker_cli image inspect \
    --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag"