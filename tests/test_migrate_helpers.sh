#!/usr/bin/env bash
# Проверки чистых функций оболочки: версия, тип базы, отсутствие DSN в разборе.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/migrate-3xui.sh"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

[[ "$(classify_release 3.4.0 yes)" == "ok v3.4.0" ]] || fail "stable"
[[ "$(classify_release v2.3.5 yes)" == "ok v2.3.5" ]] || fail "floor"
[[ "$(classify_release 2.3.4 yes)" == "too_old" ]] || fail "below floor"
[[ "$(classify_release 0.3.2 yes)" == "old" ]] || fail "x-ui"
[[ "$(classify_release dev+3.4.0 yes)" == "dev" ]] || fail "dev"
[[ "$(classify_release 3.4.0 no)" == "unknown" ]] || fail "no marker"
[[ "$(classify_release 1.8.0 yes)" == "too_old" ]] || fail "v1"

ver_lt 2.3.4 2.3.5 || fail "ver_lt less"
if ver_lt 2.3.5 2.3.5; then
  fail "ver_lt equal"
fi
if ver_lt 3.0.0 2.3.5; then
  fail "ver_lt greater"
fi

[[ "$(sanitize_db_type '')" == sqlite ]] || fail "empty type"
[[ "$(sanitize_db_type sqlite)" == sqlite ]] || fail "sqlite"
[[ "$(sanitize_db_type postgres)" == postgres ]] || fail "postgres"
secret='postgres://xui:s3cret@127.0.0.1:5432/xui?sslmode=disable'
[[ "$(sanitize_db_type "$secret")" == unsupported ]] || fail "dsn type"
case "$(sanitize_db_type "$secret")" in
  *s3cret*) fail "dsn leaked" ;;
esac

[[ "$(safe_mode 4755 600)" == 644 ]] || fail "setuid mode"
[[ "$(safe_mode 600 644)" == 600 ]] || fail "mode 600"
[[ "$(safe_mode abc 600)" == 600 ]] || fail "bad mode"
[[ "$(db_folder_status /etc/x-ui)" == yes ]] || fail "default folder"
[[ "$(db_folder_status /var/lib/x-ui)" == yes ]] || fail "custom folder"
[[ "$(db_folder_status /etc/../x-ui)" == no ]] || fail "dotdot"
[[ "$(db_folder_status 'relative')" == no ]] || fail "relative"
[[ "$(db_folder_status '/tmp/x ui')" == no ]] || fail "space"

parse_probe <<EOF
VERSION=3.4.0
MARKER=yes
DB_TYPE=sqlite
DB_FOLDER=/etc/x-ui
ENV_FILE=/etc/default/x-ui
DB_EXISTS=yes
COPY_KIND=sqlite
COPY_PATH=/tmp/xui-migrate.abc123
BINARY_OK=yes
XUI_DB_DSN=${secret}
EOF
[[ "$VERSION" == 3.4.0 ]] || fail "version parse"
[[ "$DB_FOLDER" == /etc/x-ui ]] || fail "folder parse"
if declare -p XUI_DB_DSN >/dev/null 2>&1; then
  fail "dsn assigned"
fi

[[ -x "$ROOT/migrate-3xui.sh" || -f "$ROOT/migrate-3xui.sh" ]] || fail "script missing"
if grep -n 'sshpass' "$ROOT/migrate-3xui.sh" >/dev/null; then
  fail "sshpass"
fi
if grep -n 'dev-latest' "$ROOT/migrate-3xui.sh" | grep -v 'не' >/dev/null; then
  fail "dev-latest install"
fi

printf 'ok\n'
