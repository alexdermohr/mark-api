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
# Git metadata snapshot regenerates the index from the immutable commit tree.
common=$(safe_git -C "$repo" rev-parse --path-format=absolute --git-common-dir) ||
    fail 'Git common directory unavailable'
[ -d "$common/objects" ] ||
    fail 'Git objects unavailable'
umask 077
attest_dir=$(mktemp -d /tmp/mark-api-control.XXXXXXXX) ||
    fail 'Git snapshot staging unavailable'
trap 'rm -rf -- "$attest_dir"' 0
trap 'exit 1' 1 2 3 15
mkdir -m 0700 "$attest_dir/objects" "$attest_dir/refs" ||
    fail 'Git snapshot staging unavailable'
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
attested_git read-tree "$expected" ||
    fail 'Git snapshot index unavailable'
status=$(attested_git status --porcelain --untracked-files=normal) ||
    fail 'Git snapshot status unavailable'
[ -z "$status" ] || fail 'dirty release checkout'
resolved=$(attested_git rev-parse --verify "$expected^{commit}") ||
    fail 'unknown release commit'
[ "$resolved" = "$expected" ] || fail 'invalid release commit'
# Git objects can be mutated independently of their names. Rehash captured
# commit, tree and blob bytes, storing verified copies in a private Git store
# before traversing each tree edge or passing Compose to privileged Docker.
private_git() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        GIT_GRAFT_FILE=/dev/null \
        /usr/bin/git --git-dir="$attest_dir" --work-tree="$repo" \
        -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -c diff.external= -c core.pager=cat "$@"
}
attest_object() {
    object_type=$1
    object_id=$2
    saved_path=$3
    case "$object_id" in ''|*[!0-9a-f]*) fail 'attested Git object content mismatch' ;; esac
    [ "${#object_id}" -eq 40 ] || fail 'attested Git object content mismatch'
    attested_git cat-file "$object_type" "$object_id" > "$saved_path" ||
        fail 'attested Git object content mismatch'
    observed=$(private_git hash-object -t "$object_type" --stdin < "$saved_path") ||
        fail 'attested Git object content mismatch'
    [ "$observed" = "$object_id" ] || fail 'attested Git object content mismatch'
    stored=$(private_git hash-object -w -t "$object_type" --stdin < "$saved_path") ||
        fail 'attested Git object content mismatch'
    [ "$stored" = "$object_id" ] || fail 'attested Git object content mismatch'
}
attest_object commit "$expected" "$attest_dir/commit.raw"
root_tree=$(/usr/bin/sed -n '1s/^tree \([0-9a-f]\{40\}\)$/\1/p' "$attest_dir/commit.raw")
[ "${#root_tree}" -eq 40 ] || fail 'attested Git object content mismatch'
attest_object tree "$root_tree" "$attest_dir/root-tree.raw"
docs_tree=$(private_git ls-tree "$root_tree" -- docs |
    /usr/bin/sed -n 's/^040000 tree \([0-9a-f]\{40\}\)[[:space:]]docs$/\1/p') ||
    fail 'attested Git object content mismatch'
[ "${#docs_tree}" -eq 40 ] || fail 'attested Git object content mismatch'
attest_object tree "$docs_tree" "$attest_dir/docs-tree.raw"
compose_blob=$(private_git ls-tree "$docs_tree" -- mark-api-container.compose.yaml |
    /usr/bin/sed -n 's/^100644 blob \([0-9a-f]\{40\}\)[[:space:]]mark-api-container\.compose\.yaml$/\1/p') ||
    fail 'attested Git object content mismatch'
[ "${#compose_blob}" -eq 40 ] || fail 'attested Git object content mismatch'
compose="$attest_dir/mark-api-container.compose.yaml"
attest_object blob "$compose_blob" "$compose"
# Keep the attested Compose bytes until the Docker command finishes.
# Use an empty private Docker CLI configuration: caller-selected Docker
# Compose plugins, contexts, endpoint variables and credential helpers must
# never execute with the release operator's Docker-daemon authority.
mkdir -m 0700 "$attest_dir/docker-config" ||
    fail 'private Docker configuration unavailable'
docker_cli() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        XDG_CONFIG_HOME=/nonexistent \
        DOCKER_CONFIG="$attest_dir/docker-config" \
        MARK_API_IMAGE_DIGEST="$digest" \
        /usr/bin/docker --config "$attest_dir/docker-config" \
        --host unix:///var/run/docker.sock "$@"
}
# Use only the already-attested immutable image and isolated read-only Docker
# one-shots. The volumes have been inspected by exact fixed name first.
# Never chmod/chown or initialize an existing volume from this guard.
attest_existing_volume() {
    volume_name=$1
    expected_identity=$2
    inspection_user=${expected_identity%:*}
    # A different named volume may still alias SQLite's host data through
    # driver_opts.device, bind, NFS or a custom plugin. Only default local
    # volumes are eligible for future independent UID/mount attestation.
    observed_driver=$(docker_cli volume inspect --format '{{.Driver}}' "$volume_name") ||
        fail 'existing volume driver unavailable'
    observed_options=$(docker_cli volume inspect --format '{{json .Options}}' "$volume_name") ||
        fail 'existing volume driver options unavailable'
    [ "$observed_driver" = local ] ||
        fail 'expected a local Docker volume with no driver options'
    case "$observed_options" in
        null|'{}') ;;
        *) fail 'expected a local Docker volume with no driver options' ;;
    esac
    observed_identity=$(docker_cli run --rm --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges:true \
        --user "$inspection_user" \
        --mount "type=volume,source=${volume_name},target=/volume,readonly,volume-nocopy" \
        --entrypoint /usr/bin/stat "$image" -c '%u:%g:%a' /volume) ||
        fail 'existing volume identity unavailable'
    [ "$observed_identity" = "$expected_identity" ] ||
        fail 'existing volume UID/GID or mode differs from pre-attested requirements'
}

# Fail closed if caller supplies an image ID not present on the local daemon.
format_id='{{.Id}}'
format_rev='{{index .Config.Labels "org.opencontainers.image.revision"}}'
actual_image=$(docker_cli image inspect --format "$format_id" "$image") ||
    fail 'immutable image not present locally'
[ "$actual_image" = "$image" ] || fail 'image identity mismatch'
actual_rev=$(docker_cli image inspect --format "$format_rev" "$image") ||
    fail 'image revision label missing'
[ "$actual_rev" = "$expected" ] || fail 'image revision label does not match commit'
actual_user=$(docker_cli image inspect --format '{{.Config.User}}' "$image") ||
    fail 'image user unavailable'
[ "$actual_user" = 'mark-api:mark-api' ] || fail 'image is not configured for Mark service user'
# Compose receives exclusively the checked immutable ID, never a caller's tag.
MARK_API_IMAGE_DIGEST="$digest"
export MARK_API_IMAGE_DIGEST
docker_cli compose -f "$compose" --project-name mark-api --profile live --profile backup config --quiet ||
    fail 'Compose configuration invalid'
case "$action" in
    verify)
        printf '%s\n' "mark-api: immutable image and revision verified"
        ;;
    live|backup)
        if [ "$action" = live ]; then
            # The draft now assigns SQLite to UID50046, not UID50044.
            # Real host/CDP/old-FD/MAP_SHARED/paired-ledger gates
            # are not yet independently reproduced. Keep live disabled.
            # Static Compose, CI and synthetic tests are not release proof.
            fail 'unmediated host CDP bypass blocks product live deployment'
        fi
        if [ "$action" = backup ]; then
            # A SQLite-only backup omits the independent UID-50043 ledger.
            # Until a crash-consistent paired backup/restore authority exists,
            # never publish it as a safe recoverable product snapshot.
            fail 'independent witness ledger backup is not implemented'
        fi
        # Kernel numeric UID isolation is meaningful on the host only if the
        # rootful daemon is not remapping container UIDs into a user namespace.
        namespace_security=$(docker_cli info --format '{{json .SecurityOptions}}') ||
            fail 'Docker identity mapping unavailable'
        case "$namespace_security" in
            *name=userns*|*name=rootless*) fail 'Docker UID remapping not attested' ;;
        esac
        # These checks are necessary, but not sufficient: the operator must
        # independently attest existing storage inodes, mappings and CDP rights.
        passwd_name=$(/usr/bin/getent passwd 50042 | /usr/bin/cut -d: -f1)
        group_name=$(/usr/bin/getent group 50042 | /usr/bin/cut -d: -f1)
        [ "$passwd_name" = mark-api ] && [ "$group_name" = mark-api ] ||
            fail 'exclusive host service UID/GID 50042 not reserved'
        owner_passwd=$(/usr/bin/getent passwd 50044 | /usr/bin/cut -d: -f1)
        owner_group=$(/usr/bin/getent group 50044 | /usr/bin/cut -d: -f1)
        [ "$owner_passwd" = mark-api-owner ] &&
            [ "$owner_group" = mark-api-owner ] ||
            fail 'exclusive browser executor UID/GID 50044 not reserved'
        # Backup/restore also require a retained independent ledger; it must
        # never be silently reinitialized by the regular live/backup paths.
        witness_passwd=$(/usr/bin/getent passwd 50043 | /usr/bin/cut -d: -f1)
        witness_group=$(/usr/bin/getent group 50043 | /usr/bin/cut -d: -f1)
        [ "$witness_passwd" = mark-api-witness ] &&
            [ "$witness_group" = mark-api-witness ] ||
            fail 'exclusive host witness UID/GID 50043 not reserved'
        ingress_passwd=$(/usr/bin/getent passwd 50045 | /usr/bin/cut -d: -f1)
        ingress_group=$(/usr/bin/getent group 50045 | /usr/bin/cut -d: -f1)
        [ "$ingress_passwd" = mark-api-ingress ] &&
            [ "$ingress_group" = mark-api-ingress ] ||
            fail 'exclusive host ingress UID/GID 50045 not reserved'
        storage_passwd=$(/usr/bin/getent passwd 50046 | /usr/bin/cut -d: -f1)
        storage_group=$(/usr/bin/getent group 50046 | /usr/bin/cut -d: -f1)
        [ "$storage_passwd" = mark-api-storage ] &&
            [ "$storage_group" = mark-api-storage ] ||
            fail 'exclusive host SQLite owner UID/GID 50046 not reserved'
        for name in mark-api-data-v1 mark-api-backups-v1 \
            mark-api-media-v1 mark-api-storage-socket-v1 \
            mark-api-fence-ledger-v1 mark-api-fence-runtime-socket-v1 \
            mark-api-fence-operator-socket-v1 mark-api-executor-socket-v1 \
            mark-api-ingress-socket-v1; do
            found=$(docker_cli volume inspect --format '{{.Name}}' "$name") ||
                fail 'required pre-attested external volume missing'
            [ "$found" = "$name" ] || fail 'volume identity changed'
        done
        attest_existing_volume mark-api-data-v1 50046:50046:700
        attest_existing_volume mark-api-backups-v1 50046:50046:700
        attest_existing_volume mark-api-media-v1 50044:50044:700
        attest_existing_volume mark-api-storage-socket-v1 50046:50044:2710
        attest_existing_volume mark-api-fence-ledger-v1 50043:50043:700
        attest_existing_volume mark-api-fence-runtime-socket-v1 50043:50044:2710
        attest_existing_volume mark-api-fence-operator-socket-v1 50043:50043:700
        attest_existing_volume mark-api-executor-socket-v1 50044:50042:2710
        attest_existing_volume mark-api-ingress-socket-v1 50042:50045:2710
        ledger_identity=$(docker_cli run --rm --network none --read-only \
            --cap-drop ALL --security-opt no-new-privileges:true \
            --user 50043:50043 \
            --mount "type=volume,source=mark-api-fence-ledger-v1,target=/volume,readonly,volume-nocopy" \
            --entrypoint /usr/bin/stat "$image" -c '%u:%g:%a:%h' /volume/ledger.sqlite) ||
            fail 'previously initialized witness ledger unavailable'
        [ "$ledger_identity" = 50043:50043:600:1 ] ||
            fail 'ledger inode owner, mode or hardlink count unsafe'
        if [ "$action" = live ]; then
            # Future sequence only. The early unverified-CDP guard prevents
            # this path from running until real host-bound evidence exists.
            docker_cli compose -f "$compose" --project-name mark-api --profile live \
                up --no-deps --no-build --pull never --detach mark-api-witness ||
                fail 'independent witness failed to start'
            witness_id=$(docker_cli compose -f "$compose" --project-name mark-api \
                --profile live ps -q mark-api-witness) ||
                fail 'independent witness identity unavailable'
            [ -n "$witness_id" ] || fail 'independent witness not running'
            observed_user=$(docker_cli inspect --format '{{.Config.User}}' "$witness_id") ||
                fail 'witness UID unavailable'
            observed_image=$(docker_cli inspect --format '{{.Image}}' "$witness_id") ||
                fail 'witness image unavailable'
            [ "$observed_user" = '50043:50043' ] &&
                [ "$observed_image" = "$image" ] ||
                fail 'witness identity mismatch'
            witness_health=starting
            attempts=0
            while [ "$attempts" -lt 20 ]; do
                witness_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$witness_id") ||
                    fail 'independent witness health unavailable'
                [ "$witness_health" = healthy ] && break
                [ "$witness_health" = starting ] ||
                    fail 'independent witness not healthy'
                attempts=$((attempts + 1))
                /usr/bin/sleep 1
            done
            [ "$witness_health" = healthy ] ||
                fail 'independent witness did not become healthy'

            docker_cli compose -f "$compose" --project-name mark-api --profile live \
                up --no-deps --no-build --pull never --detach mark-api-storage ||
                fail 'independent SQLite owner failed to start'
            storage_id=$(docker_cli compose -f "$compose" --project-name mark-api \
                --profile live ps -q mark-api-storage) ||
                fail 'SQLite owner identity unavailable'
            [ -n "$storage_id" ] || fail 'SQLite owner not running'
            storage_user=$(docker_cli inspect --format '{{.Config.User}}' "$storage_id") ||
                fail 'SQLite owner UID unavailable'
            storage_image=$(docker_cli inspect --format '{{.Image}}' "$storage_id") ||
                fail 'SQLite owner image unavailable'
            storage_network=$(docker_cli inspect --format '{{.HostConfig.NetworkMode}}' "$storage_id") ||
                fail 'SQLite owner network unavailable'
            storage_mounts=$(docker_cli inspect --format '{{range .Mounts}}{{.Name}};{{end}}' "$storage_id") ||
                fail 'SQLite owner mounts unavailable'
            [ "$storage_user" = '50046:50046' ] &&
                [ "$storage_image" = "$image" ] &&
                [ "$storage_network" = none ] ||
                fail 'SQLite owner UID, image or network mismatch'
            case "$storage_mounts" in
                *mark-api-data-v1*mark-api-storage-socket-v1*|*mark-api-storage-socket-v1*mark-api-data-v1*) ;;
                *) fail 'SQLite owner has no attested data and socket mounts' ;;
            esac
            case "$storage_mounts" in
                *mark-api-executor-socket-v1*|*mark-api-media-v1*|*mark-api-fence-*)
                    fail 'SQLite owner has forbidden browser, media or Witness mounts' ;;
            esac
            storage_health=starting
            attempts=0
            while [ "$attempts" -lt 20 ]; do
                storage_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$storage_id") ||
                    fail 'SQLite owner health unavailable'
                [ "$storage_health" = healthy ] && break
                [ "$storage_health" = starting ] ||
                    fail 'SQLite owner not healthy'
                attempts=$((attempts + 1))
                /usr/bin/sleep 1
            done
            [ "$storage_health" = healthy ] ||
                fail 'independent SQLite owner socket unavailable'

            docker_cli compose -f "$compose" --project-name mark-api --profile live \
                up --no-deps --no-build --pull never --detach mark-api-owner ||
                fail 'browser executor failed to start'
            owner_id=$(docker_cli compose -f "$compose" --project-name mark-api \
                --profile live ps -q mark-api-owner) ||
                fail 'browser executor identity unavailable'
            [ -n "$owner_id" ] || fail 'browser executor not running'
            observed_owner_user=$(docker_cli inspect --format '{{.Config.User}}' "$owner_id") ||
                fail 'executor UID unavailable'
            observed_owner_image=$(docker_cli inspect --format '{{.Image}}' "$owner_id") ||
                fail 'executor image unavailable'
            observed_owner_network=$(docker_cli inspect --format '{{.HostConfig.NetworkMode}}' "$owner_id") ||
                fail 'executor network unavailable'
            [ "$observed_owner_user" = '50044:50044' ] &&
                [ "$observed_owner_image" = "$image" ] &&
                [ "$observed_owner_network" = host ] ||
                fail 'executor UID, image or host network mismatch'
            observed_owner_mounts=$(docker_cli inspect --format '{{range .Mounts}}{{.Name}};{{end}}' "$owner_id") ||
                fail 'executor mount identities unavailable'
            case "$observed_owner_mounts" in
                *mark-api-data-v1*|*mark-api-backups-v1*|*mark-api-fence-ledger-v1*)
                    fail 'executor retained raw SQLite, backup or Witness authority' ;;
            esac
            case "$observed_owner_mounts" in
                *mark-api-storage-socket-v1*mark-api-media-v1*|*mark-api-media-v1*mark-api-storage-socket-v1*) ;;
                *) fail 'executor independent storage socket or media directory missing' ;;
            esac
            owner_health=starting
            attempts=0
            while [ "$attempts" -lt 20 ]; do
                owner_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$owner_id") ||
                    fail 'executor health unavailable'
                [ "$owner_health" = healthy ] && break
                [ "$owner_health" = starting ] ||
                    fail 'executor not healthy'
                attempts=$((attempts + 1))
                /usr/bin/sleep 1
            done
            [ "$owner_health" = healthy ] ||
                fail 'executor did not become healthy'

            docker_cli compose -f "$compose" --project-name mark-api --profile live \
                up --no-deps --no-build --pull never --detach mark-api ||
                fail 'product frontend failed to start'
            product_id=$(docker_cli compose -f "$compose" --project-name mark-api \
                --profile live ps -q mark-api) ||
                fail 'frontend identity unavailable'
            [ -n "$product_id" ] || fail 'product frontend not running'
            observed_product_user=$(docker_cli inspect --format '{{.Config.User}}' "$product_id") ||
                fail 'frontend UID unavailable'
            observed_product_image=$(docker_cli inspect --format '{{.Image}}' "$product_id") ||
                fail 'frontend image unavailable'
            observed_product_running=$(docker_cli inspect --format '{{.State.Running}}' "$product_id") ||
                fail 'frontend running state unavailable'
            observed_product_network=$(docker_cli inspect --format '{{.HostConfig.NetworkMode}}' "$product_id") ||
                fail 'frontend network unavailable'
            observed_product_mounts=$(docker_cli inspect --format '{{range .Mounts}}{{.Name}};{{end}}' "$product_id") ||
                fail 'frontend mount identities unavailable'
            [ "$observed_product_network" = none ] ||
                fail 'frontend has IP-network/CDP authority'
            case "$observed_product_mounts" in
                *mark-api-data-v1*|*mark-api-backups-v1*|*mark-api-fence-*)
                    fail 'frontend has forbidden recovery or data volume' ;;
            esac
            [ "$observed_product_user" = '50042:50042' ] &&
                [ "$observed_product_image" = "$image" ] &&
                [ "$observed_product_running" = true ] ||
                fail 'frontend UID, image or running state mismatch'
            product_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$product_id") ||
                fail 'netless frontend health unavailable'
            attempts=0
            while [ "$product_health" = starting ] && [ "$attempts" -lt 20 ]; do
                attempts=$((attempts + 1))
                /usr/bin/sleep 1
                product_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$product_id") ||
                    fail 'netless frontend health unavailable'
            done
            [ "$product_health" = healthy ] ||
                fail 'netless frontend Unix socket unavailable'

            docker_cli compose -f "$compose" --project-name mark-api --profile live \
                up --no-deps --no-build --pull never --detach mark-api-ingress ||
                fail 'dashboard public ingress failed to start'
            ingress_id=$(docker_cli compose -f "$compose" --project-name mark-api \
                --profile live ps -q mark-api-ingress) ||
                fail 'dashboard public ingress identity unavailable'
            [ -n "$ingress_id" ] || fail 'dashboard public ingress not running'
            ingress_user=$(docker_cli inspect --format '{{.Config.User}}' "$ingress_id") ||
                fail 'dashboard public ingress UID unavailable'
            ingress_image=$(docker_cli inspect --format '{{.Image}}' "$ingress_id") ||
                fail 'dashboard public ingress image unavailable'
            ingress_network=$(docker_cli inspect --format '{{.HostConfig.NetworkMode}}' "$ingress_id") ||
                fail 'dashboard public ingress network unavailable'
            ingress_mounts=$(docker_cli inspect --format '{{range .Mounts}}{{.Name}};{{end}}' "$ingress_id") ||
                fail 'dashboard public ingress mounts unavailable'
            [ "$ingress_user" = 50045:50045 ] &&
                [ "$ingress_image" = "$image" ] &&
                [ "$ingress_network" = host ] ||
                fail 'dashboard public ingress UID/image/network mismatch'
            case "$ingress_mounts" in
                *mark-api-data-v1*|*mark-api-backups-v1*|*mark-api-executor-socket-v1*|*mark-api-fence-*)
                    fail 'dashboard ingress has forbidden data, browser or recovery authority' ;;
            esac
            ingress_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$ingress_id") ||
                fail 'dashboard public ingress health unavailable'
            attempts=0
            while [ "$ingress_health" = starting ] && [ "$attempts" -lt 20 ]; do
                attempts=$((attempts + 1))
                /usr/bin/sleep 1
                ingress_health=$(docker_cli inspect --format '{{.State.Health.Status}}' "$ingress_id") ||
                    fail 'dashboard public ingress health unavailable'
            done
            [ "$ingress_health" = healthy ] ||
                fail 'dashboard public ingress loopback unavailable'
            exit 0
        fi
        docker_cli compose -f "$compose" --project-name mark-api --profile backup \
            run --rm --no-deps --pull never mark-api-backup "$instance"
        ;;
esac
