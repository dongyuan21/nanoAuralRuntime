#!/bin/sh
set -eu

source_secret=/run/secrets/postgres_runtime_password
staged_dir=/run/nano-aural-secrets
staged_secret=$staged_dir/postgres_runtime_password
staged_temporary=$staged_dir/.postgres_runtime_password.tmp

fail() {
    echo "postgres secret staging failed; check mounted secret" >&2
    exit 1
}

cleanup() {
    rm -f -- "$staged_temporary"
}
trap cleanup EXIT HUP INT TERM

[ "$(id -u)" -eq 0 ] || fail
[ -f "$source_secret" ] && [ ! -L "$source_secret" ] || fail
case "$(stat -c '%a' "$source_secret")" in
    400|600) ;;
    *) fail ;;
esac

install -d -o 0 -g 999 -m 0710 "$staged_dir"
exec 3< "$source_secret"
dd bs=4097 count=1 <&3 > "$staged_temporary" 2>/dev/null || fail
exec 3<&-
[ -f "$staged_temporary" ] && [ ! -L "$staged_temporary" ] || fail
staged_size=$(wc -c < "$staged_temporary")
if [ "$staged_size" -lt 1 ] || [ "$staged_size" -gt 4096 ]; then
    fail
fi
chown 999:999 "$staged_temporary"
chmod 0400 "$staged_temporary"
mv -f "$staged_temporary" "$staged_secret"
trap - EXIT HUP INT TERM

exec /usr/local/bin/docker-entrypoint.sh "$@"
