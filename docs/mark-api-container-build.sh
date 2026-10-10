#!/bin/sh
# Build a container solely from one clean, exact Git commit.
set -eu
# Never resolve a reviewed SHA through replacement objects or legacy grafts.
# Local Git config inherited from caller must not redirect the object database.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
unset GIT_ALTERNATE_OBJECT_DIRECTORIES
export GIT_NO_REPLACE_OBJECTS=1
export GIT_GRAFT_FILE=/dev/null
[ "$#" -eq 1 ] || { printf '%s\n' 'usage: sh docs/mark-api-container-build.sh EXACT_HEAD_SHA' >&2; exit 64; }
expected=$1
case "$expected" in
    *[!0-9a-f]*|'') exit 64 ;;
esac
[ "${#expected}" -eq 40 ] || exit 64
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
head=$(git -C "$repo" rev-parse --verify HEAD)
[ "$head" = "$expected" ] || { printf '%s\n' 'mark-api: HEAD changed' >&2; exit 1; }
[ -z "$(git -C "$repo" status --porcelain --untracked-files=normal)" ] || {
    printf '%s\n' 'mark-api: source checkout is dirty' >&2
    exit 1
}
# Reject even inactive replacement/graft metadata: no ambiguous revision trust.
[ -z "$(git -C "$repo" for-each-ref --format='%(refname)' refs/replace)" ] || {
    printf '%s\n' 'mark-api: Git replacement references are not allowed' >&2
    exit 1
}
common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)
[ ! -e "$common/info/grafts" ] && [ ! -L "$common/info/grafts" ] || {
    printf '%s\n' 'mark-api: Git grafts are not allowed' >&2
    exit 1
}
[ -z "${DOCKER_HOST-}" ] && [ -z "${DOCKER_CONTEXT-}" ] || {
    printf '%s\n' 'mark-api: remote/custom Docker targets require separate attestation' >&2
    exit 1
}
temp=$(mktemp -d "${TMPDIR:-/tmp}/mark-api-image.XXXXXXXX") || exit 1
trap 'rm -rf -- "$temp"' 0
trap 'exit 1' 1 2 3 15
mkdir -m 0700 "$temp/source" "$temp/context" "$temp/docs"
git -C "$repo" archive "$expected" -- pyproject.toml README.md src |
    tar -xf - -C "$temp/source"
"${MARK_UV:-uv}" build --wheel --offline --out-dir "$temp/context" "$temp/source"
set -- "$temp/context"/*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || {
    printf '%s\n' 'mark-api: expected exactly one verified wheel' >&2
    exit 1
}
git -C "$repo" archive "$expected" -- \
    docs/mark-api-container.Dockerfile \
    docs/mark-api-container-start.sh \
    docs/mark-api-container-backup.sh \
    docs/mark-api-bootstrap.sh docs/mark-api-preflight.py |
    tar -xf - -C "$temp"
for file in mark-api-container.Dockerfile mark-api-container-start.sh \
            mark-api-container-backup.sh mark-api-bootstrap.sh \
            mark-api-preflight.py; do
    cp -- "$temp/docs/$file" "$temp/context/$file"
done
short=$(printf '%.12s' "$expected")
tag="mark-api:pr75-$short"
/usr/bin/docker --host unix:///var/run/docker.sock build --pull=false \
    --label "org.opencontainers.image.revision=$expected" \
    --file "$temp/context/mark-api-container.Dockerfile" --tag "$tag" "$temp/context"
/usr/bin/docker --host unix:///var/run/docker.sock image inspect \
    --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag"