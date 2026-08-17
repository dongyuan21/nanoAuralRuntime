#!/bin/sh
set -eu

secret=/run/nano-aural-secrets/postgres_runtime_password
if [ ! -f "$secret" ] || [ -L "$secret" ]; then
    echo "runtime role initialization failed; check mounted secret" >&2
    exit 1
fi
if [ "$(stat -c '%u' "$secret")" -ne "$(id -u)" ] || [ "$(stat -c '%a' "$secret")" != 400 ]; then
    echo "runtime role initialization failed; check mounted secret" >&2
    exit 1
fi
secret_size=$(wc -c < "$secret")
if [ "$secret_size" -lt 1 ] || [ "$secret_size" -gt 4096 ]; then
    echo "runtime role initialization failed; check mounted secret" >&2
    exit 1
fi
non_nul_size=$(tr -d '\000' < "$secret" | wc -c)
if [ "$non_nul_size" -ne "$secret_size" ]; then
    echo "runtime role initialization failed; check mounted secret" >&2
    exit 1
fi

exec 3< "$secret"
runtime_password=
IFS= read -r runtime_password <&3 || [ -n "$runtime_password" ]
_unexpected_second_line=
if IFS= read -r _unexpected_second_line <&3 || [ -n "$_unexpected_second_line" ]; then
    exec 3<&-
    echo "runtime role initialization failed; check mounted secret" >&2
    exit 1
fi
exec 3<&-
carriage_return=$(printf '\r')
case "$runtime_password" in
    ''|*"$carriage_return"*)
        echo "runtime role initialization failed; check mounted secret" >&2
        exit 1
        ;;
esac

NANO_AURAL_RUNTIME_PASSWORD=$runtime_password
export NANO_AURAL_RUNTIME_PASSWORD
psql --set=ON_ERROR_STOP=1 --no-psqlrc \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv runtime_password NANO_AURAL_RUNTIME_PASSWORD
SELECT pg_catalog.format(
    'CREATE ROLE nano_aural_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='nano_aural_runtime'
) \gexec
SELECT pg_catalog.format(
    'ALTER ROLE nano_aural_runtime WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_password'
) \gexec
\unset runtime_password
SQL
unset NANO_AURAL_RUNTIME_PASSWORD runtime_password
