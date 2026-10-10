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
# Require a direct root-owned OS interpreter symlink. Checking only the
# readlink -f destination overlooks a replaceable intermediate symlink.
python_link="$os_root/bin/python3"
[ -L "$python_link" ] || fail
unsafe=$(/usr/bin/find -P "$python_link" -maxdepth 0 \
    \( ! -uid 0 -o ! -type l \) -print -quit) || fail
[ -z "$unsafe" ] || fail
link_target=$(/usr/bin/readlink "$python_link") || fail
base=${link_target##*/}
minor=${base#python3.}
case "$base" in
    python3.*) ;;
    *) fail ;;
esac
case "$minor" in
    ''|*[!0-9]*) fail ;;
esac
case "$link_target" in
    "$base"|"$os_root/bin/$base") ;;
    *) fail ;;
esac
base_py="$os_root/bin/$base"
trusted "$base_py"
resolved=$(/usr/bin/readlink -f "$python_link") || fail
[ "$resolved" = "$base_py" ] || fail

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
        resolved=$(/usr/bin/readlink -f "$link") || exit 1
        [ -n "$resolved" ] || exit 1
        next=$link
        depth=0
        while :; do
            depth=$((depth + 1))
            [ "$depth" -le 40 ] || exit 1
            # Check the link inode itself, not only its dereferenced target.
            unsafe=$(/usr/bin/find -P "$next" -maxdepth 0 \
                \( ! -uid 0 -o ! -type l \) -print -quit) || exit 1
            [ -z "$unsafe" ] || exit 1
            raw=$(/usr/bin/readlink "$next") || exit 1
            case "$raw" in
                /*) walk=/ ;;
                *) walk="${next%/*}" ;;
            esac
            old_ifs=$IFS
            IFS=/
            set -f
            set -- $raw
            [ "$#" -gt 0 ] || exit 1
            while [ "$#" -gt 0 ]; do
                part=$1
                shift
                case "$part" in
                    ""|".") continue ;;
                    "..") walk=${walk%/*}; [ -n "$walk" ] || walk=/ ;;
                    *) case "$walk" in
                        /) walk="/$part" ;;
                        *) walk="$walk/$part" ;;
                       esac ;;
                esac
                # Only a terminal link may extend the attested chain.
                # A symlink inside the lexical path is always unsafe.
                if [ -L "$walk" ]; then
                    [ "$#" -eq 0 ] || exit 1
                else
                    unsafe=$(/usr/bin/find -P "$walk" -maxdepth 0 \
                        \( ! -uid 0 -o -perm /022 -o \( ! -type d -a ! -type f \) \) \
                        -print -quit) || exit 1
                    [ -z "$unsafe" ] || exit 1
                    [ "$#" -eq 0 ] || [ -d "$walk" ] || exit 1
                fi
            done
            set +f
            IFS=$old_ifs
            if [ -L "$walk" ]; then
                next=$walk
                continue
            fi
            [ "$walk" = "$resolved" ] || exit 1
            path=$walk
            while :; do
                unsafe=$(/usr/bin/find -P "$path" -maxdepth 0 \
                    \( ! -uid 0 -o -perm /022 -o \( ! -type d -a ! -type f \) \) \
                    -print -quit) || exit 1
                [ -z "$unsafe" ] || exit 1
                [ "$path" = / ] && break
                path=${path%/*}
                [ -n "$path" ] || path=/
            done
            break
        done
    done
' _ {} + || fail

# CPython can import a stdlib ZIP before any Python-level checks.
archive="$os_root/lib/python3${minor}.zip"
if [ -e "$archive" ] || [ -L "$archive" ]; then
    [ ! -L "$archive" ] || fail
    trusted "$archive"
fi

# The Python preflight script itself executes before its Python-level
# validation, so verify its own file and parent chain natively first.
for path in /etc /etc/mark-api; do
    trusted "$path"
done
trusted "/etc/mark-api/bootstrap.sh"
trusted "/etc/mark-api/preflight.py"

# The Python-level guard now checks the installed venv and private SQLite
# paths before mark_api.launcher or mark_api.backup_cli are imported.
exec /usr/bin/python3 -I -S /etc/mark-api/preflight.py "$@"