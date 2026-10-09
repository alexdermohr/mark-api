#!/bin/sh
# Native first-code check, installed root:root 0644 under /etc/mark-api.
# This precedes *any* Python stdlib import by mark-api's ExecStartPre.
# The OS-managed /bin/sh, /usr/bin/find, /usr/bin/readlink, their loader
# and root-managed systemd unit are the explicit native trust base.
set -eu

fail() {
    printf '%s\n' 'mark-api: OS Python bootstrap trust boundary failed' >&2
    exit 1
}

# Deliberately hard-coded: the service account cannot choose an alternate
# interpreter, base path, root UID or an unscanned native code path.
os_root=/usr

trusted() {
    [ -e "$1" ] || fail
    # GNU find -P does not follow final symlinks; callers pass canonical
    # targets and separately verify all canonical ancestor directories.
    unsafe=$(/usr/bin/find -P "$1" -maxdepth 0 \
        \( ! -uid 0 -o -perm /022 -o \( ! -type d -a ! -type f \) \) \
        -print -quit) || fail
    [ -z "$unsafe" ] || fail
}

for path in / "$os_root" "$os_root/bin" "$os_root/lib"; do
    trusted "$path"
done
trusted "$os_root/bin/readlink"
trusted "$os_root/bin/find"
base_py=$(/usr/bin/readlink -f "$os_root/bin/python3") || fail
case "$base_py" in
    "$os_root"/bin/python3.*) ;;
    *) fail ;;
esac
base=${base_py##*/}
minor=${base#python3.}
case "$minor" in
    ''|*[!0-9]*) fail ;;
esac
trusted "$base_py"

stdlib="$os_root/lib/$base"
[ -d "$stdlib" ] && [ ! -L "$stdlib" ] || fail
trusted "$stdlib"
# Any non-root owner, service-writable byte, unusual inode or unsafe
# symlink target aborts before Python is launched. Check traverse errors.
unsafe=$(/usr/bin/find -L "$stdlib" \
    \( ! -uid 0 -o -perm /022 -o \( ! -type d -a ! -type f \) \) \
    -print -quit) || fail
[ -z "$unsafe" ] || fail

# find -L audits symlink targets, but not their outer parent chains.
# Audit every resolved symlink target's parents to exclude replaceable
# external paths, for example the distro's /etc/pythonX.Y/sitecustomize.py.
# GNU find -exec ... {} + preserves arbitrary filenames, including embedded
# newlines; line-oriented shell word splitting cannot be a safety boundary.
/usr/bin/find -P "$stdlib" -type l -exec /bin/sh -c '
    set -eu
    for link do
        path=$(/usr/bin/readlink -f "$link") || exit 1
        [ -n "$path" ] || exit 1
        while :; do
            unsafe=$(/usr/bin/find -P "$path" -maxdepth 0 \
                \( ! -uid 0 -o -perm /022 -o \( ! -type d -a ! -type f \) \) \
                -print -quit) || exit 1
            [ -z "$unsafe" ] || exit 1
            [ "$path" = / ] && break
            path=${path%/*}
            [ -n "$path" ] || path=/
        done
    done
' _ {} + || fail

# CPython can import a stdlib ZIP before any Python-level checks.
archive="$os_root/lib/python3${minor}.zip"
if [ -e "$archive" ] || [ -L "$archive" ]; then
    [ ! -L "$archive" ] || fail
    trusted "$archive"
fi

# The Python-level guard now checks the installed venv and private SQLite
# paths before mark_api.launcher or mark_api.backup_cli are imported.
exec /usr/bin/python3 -I -S /etc/mark-api/preflight.py "$@"