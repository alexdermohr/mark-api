#!/bin/sh
# Build a container solely from one clean, exact Git commit.
set -eu
# Do not resolve even the first dirname/mktemp/cp through caller PATH.
# GNU tar honors TAR_OPTIONS (including executable checkpoint actions).
PATH=/usr/bin:/bin
export PATH
# This cannot undo code already loaded before the shell entered this file;
# use a trusted/sanitized operator launch as documented in the runbook.
unset TAR_OPTIONS SOURCE_DATE_EPOCH LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG
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
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || { printf '%s\n' 'usage: sh docs/mark-api-container-build.sh EXACT_HEAD_SHA [PINNED_SETUPTOOLS_WHEEL]' >&2; exit 64; }
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
# The reviewed build backend must be supplied offline, not resolved from a
# mutable registry or inherited user-site packages. Copy the caller artifact
# into our private directory and authenticate its bytes *after* the copy.
setuptools_source=${2:-${MARK_SETUPTOOLS_WHEEL:-}}
[ -n "$setuptools_source" ] && [ -f "$setuptools_source" ] &&
    [ ! -L "$setuptools_source" ] ||
    fail 'pinned setuptools wheel is required (MARK_SETUPTOOLS_WHEEL)'
setuptools_wheel="$temp/setuptools-80.9.0-py3-none-any.whl"
cp -- "$setuptools_source" "$setuptools_wheel" ||
    fail 'cannot copy pinned setuptools wheel'
wheel_sha=$(/usr/bin/sha256sum "$setuptools_wheel") ||
    fail 'cannot hash pinned setuptools wheel'
wheel_sha=${wheel_sha%% *}
[ "$wheel_sha" = "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922" ] ||
    fail 'pinned setuptools wheel checksum mismatch'

# Permit only the build-controlled process environment. Inherited UV_* flags
# (notably UV_VENV_SEED and UV_FIND_LINKS), DIST_EXTRA_CONFIG, user HOME and
# setuptools distutils settings are not trusted execution inputs. A pinned
# version and --no-config alone do not suppress environment-variable options.
build_home=$(/usr/bin/getent passwd "$(/usr/bin/id -u)" | /usr/bin/cut -d: -f6) ||
    fail 'trusted operator account unavailable'
case "$build_home" in /*) ;; *) fail 'trusted operator account home invalid' ;; esac
# A caller-supplied MARK_UV executable is arbitrary code, even behind env -i.
# Accept only the fixed operator toolchain path, then freeze and hash its bytes
# inside this create-only private staging directory before first execution.
trusted_uv="$build_home/.local/bin/uv"
case ${MARK_UV-} in ""|"$trusted_uv") ;; *)
    fail 'untrusted uv binary override' ;;
esac
[ -f "$trusted_uv" ] && [ ! -L "$trusted_uv" ] && [ -x "$trusted_uv" ] ||
    fail 'trusted uv executable unavailable'
mkdir -m 0700 "$temp/build-home" "$temp/uv-cache" "$temp/toolchain" ||
    fail 'private build environment unavailable'
build_uv="$temp/toolchain/uv"
/usr/bin/install -m 0500 "$trusted_uv" "$build_uv" ||
    fail 'trusted uv executable copy failed'
build_uv_sha=$(/usr/bin/sha256sum "$build_uv") ||
    fail 'trusted uv executable hash unavailable'
build_uv_sha=${build_uv_sha%% *}
[ "$build_uv_sha" = "534bcd0cf0a0d00a0977363abfc93cd1300914d03e16a77d8d5133a3fd99dacb" ] ||
    fail 'trusted uv executable checksum mismatch'
uv_cli() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME="$temp/build-home" \
        XDG_CONFIG_HOME="$temp/build-home" XDG_CACHE_HOME="$temp/uv-cache" \
        UV_CACHE_DIR="$temp/uv-cache" UV_LINK_MODE=copy \
        UV_PYTHON_INSTALL_DIR="$build_home/.local/share/uv/python" \
        PYTHONNOUSERSITE=1 SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH-}" \
        "$build_uv" --no-config "$@"
}
uv_cli venv --offline --no-project --no-python-downloads \
    --python 3.12.12 "$temp/build-venv" ||
    fail 'fixed Python 3.12.12 build environment unavailable'
uv_cli pip install --offline --no-index --no-deps \
    --python "$temp/build-venv/bin/python" "$setuptools_wheel" ||
    fail 'verified build backend installation failed'
/usr/bin/env -i PATH=/usr/bin:/bin HOME="$temp/build-home" \
    XDG_CONFIG_HOME="$temp/build-home" PYTHONNOUSERSITE=1 \
    "$temp/build-venv/bin/python" -I -c \
    'import importlib.metadata as md; assert md.version("setuptools") == "80.9.0"' ||
    fail 'build backend version unverified'
# Git's history-walking 'show' would follow the commit's unneeded
# parent outside this deliberately minimal, hash-verified object store.
# Derive the epoch strictly from the verified commit object header instead.
verified_commit=$(private_git cat-file commit "$expected") ||
    fail 'attested commit timestamp unavailable'
SOURCE_DATE_EPOCH=$(printf '%s\n' "$verified_commit" | /usr/bin/awk '
    $0 == "" { exit }
    $1 == "committer" {
        if (NF >= 3 && $(NF - 1) ~ /^[0-9]+$/ &&
                $NF ~ /^[+-][0-9][0-9][0-9][0-9]$/) {
            print $(NF - 1)
            found = 1
        }
        exit
    }
    END { if (!found) exit 1 }
') || fail 'attested commit timestamp unavailable'
export SOURCE_DATE_EPOCH
uv_cli build --wheel --offline --no-build-isolation \
    --python "$temp/build-venv/bin/python" \
    --out-dir "$temp/context" "$temp/source" ||
    fail 'verified offline wheel build failed'
set -- "$temp/context"/*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || {
    printf '%s\n' 'mark-api: expected exactly one verified wheel' >&2
    exit 1
}
private_git archive "$expected" -- \
    docs/mark-api-container.Dockerfile \
    docs/mark-api-container-start.sh \
    docs/mark-api-container-frontend.sh \
    docs/mark-api-container-ingress.sh \
    docs/mark-api-container-ingress-landlock.py \
    docs/mark-api-container-witness.sh \
    docs/mark-api-container-storage.sh \
    docs/mark-api-container-healthcheck.py \
    docs/mark-api-container-backup.sh \
    docs/mark-api-bootstrap.sh docs/mark-api-preflight.py > "$temp/docs.tar" ||
    fail 'verified Docker files unavailable'
/usr/bin/tar -xf "$temp/docs.tar" -C "$temp" ||
    fail 'verified Docker files extraction failed'
for file in mark-api-container.Dockerfile mark-api-container-start.sh \
            mark-api-container-frontend.sh mark-api-container-ingress.sh \
            mark-api-container-ingress-landlock.py \
            mark-api-container-witness.sh \
            mark-api-container-storage.sh \
            mark-api-container-healthcheck.py \
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