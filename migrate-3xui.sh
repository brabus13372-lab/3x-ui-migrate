#!/usr/bin/env bash
# Интерактивный перенос 3x-ui (MHSanaei) на этот сервер.
# Запускать от root уже на новом сервере. Исходный сервер только читается.
set -euo pipefail
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PY="${SCRIPT_DIR}/strip_clients.py"

DRY_RUN=0
SUCCESS=0
WORK=""
REMOTE_TMP=""
SSH_BASE=()
SSH_HOST=""
SSH_USER=""
SSH_PORT=""
SSH_KEY=""
REPLACEMENTS=()

usage() {
  cat <<'EOF'
Использование: migrate-3xui.sh [--dry-run]

Запускать на новом сервере от root, в интерактивном терминале.
  1  чистая установка последней стабильной 3x-ui
  2  миграция с другого сервера по SSH-ключу

--dry-run  для миграции: снять копию, посчитать и показать план,
           ничего не ставить и базу на этом сервере не подменять.
EOF
}

die() {
  printf '%s\n' "$1" >&2
  exit "${2:-1}"
}

info() {
  printf '%s\n' "$1"
}

warn() {
  printf 'ВНИМАНИЕ: %s\n' "$1" >&2
}

confirm_yes() {
  local answer
  read -r -p "$1" answer || return 1
  [[ "$answer" == "yes" ]]
}

ver_lt() {
  local a=$1 b=$2
  local IFS=.
  local -a aa bb
  read -r -a aa <<<"${a}"
  read -r -a bb <<<"${b}"
  local i x y
  for i in 0 1 2; do
    x=${aa[$i]:-0}
    y=${bb[$i]:-0}
    if ((10#$x < 10#$y)); then
      return 0
    fi
    if ((10#$x > 10#$y)); then
      return 1
    fi
  done
  return 1
}

# Печатает одно слово: ok vX.Y.Z | old | dev | too_old | unknown
classify_release() {
  local ver=$1 marker=$2 bare major
  if [[ "$ver" == *dev* || "$ver" == *DEV* || "$ver" == *Dev* ]]; then
    printf 'dev\n'
    return 0
  fi
  bare=${ver#v}
  bare=${bare#V}
  if [[ ! "$bare" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'unknown\n'
    return 0
  fi
  major=${bare%%.*}
  if ((major == 0)); then
    printf 'old\n'
    return 0
  fi
  if [[ "$marker" != yes ]]; then
    printf 'unknown\n'
    return 0
  fi
  if ver_lt "$bare" "2.3.5"; then
    printf 'too_old\n'
    return 0
  fi
  printf 'ok v%s\n' "$bare"
}

sanitize_db_type() {
  case "$1" in
    "" | sqlite | SQLite | SQLITE) printf 'sqlite\n' ;;
    postgres | postgresql | Postgres | PostgreSQL) printf 'postgres\n' ;;
    *) printf 'unsupported\n' ;;
  esac
}

db_folder_status() {
  case "$1" in
    *..* | *[!/A-Za-z0-9._-]*) printf 'no\n' ;;
    /*) printf 'yes\n' ;;
    *) printf 'no\n' ;;
  esac
}

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "Скрипт запускается от root на новом сервере."
}

require_tty() {
  [[ -t 0 && -t 1 ]] || die "Нужен интерактивный терминал: скрипт спрашивает режим и подтверждение."
}

require_python() {
  command -v python3 >/dev/null 2>&1 || die "Нужен Python 3."
  python3 -c 'import sqlite3' >/dev/null 2>&1 || die "У Python 3 нет модуля sqlite3."
  [[ -f "$PY" ]] || die "Рядом со скриптом нет strip_clients.py."
}

py() {
  python3 "$PY" "$@"
}

on_exit() {
  local status=$?
  cleanup_remote
  if [[ -n "$WORK" && -d "$WORK" ]]; then
    if [[ "$SUCCESS" -eq 1 || ! -f "$WORK/panel.db" ]]; then
      rm -rf -- "$WORK"
    else
      printf 'Рабочий каталог с копией базы оставлен: %s\n' "$WORK" >&2
      printf 'В нём секреты панели. Удалите его, когда разберётесь с ошибкой.\n' >&2
    fi
  fi
  if [[ -d /root/.xui-migrate.lock ]]; then
    rmdir /root/.xui-migrate.lock 2>/dev/null || true
  fi
  exit "$status"
}

take_lock() {
  if ! mkdir /root/.xui-migrate.lock 2>/dev/null; then
    die "Уже выполняется другой запуск (есть /root/.xui-migrate.lock)."
  fi
}

cleanup_remote() {
  [[ -n "${REMOTE_TMP}" ]] || return 0
  [[ ${#SSH_BASE[@]} -gt 0 ]] || return 1
  local q i
  q=$(printf '%q' "$REMOTE_TMP")
  for i in 1 2 3; do
    if "${SSH_BASE[@]}" "rm -f -- ${q}"; then
      REMOTE_TMP=""
      return 0
    fi
    sleep 1
  done
  warn "Не удалось удалить временную копию на источнике: ${REMOTE_TMP}. В ней полная база. Удалите файл вручную."
  return 1
}

safe_mode() {
  local raw=$1 fallback=$2 dec
  if [[ ! "$raw" =~ ^[0-7]{3,4}$ ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  dec=$((8#$raw & 0644))
  printf '%o\n' "$dec"
}

refuse_if_local_target() {
  local host=$1 ip ips
  case "$host" in
    localhost | 127.0.0.1 | ::1 | "[::1]")
      die "Источник — этот же сервер. Так скрипт не переносит: исходная панель должна остаться работать."
      ;;
  esac
  ips=$(hostname -I 2>/dev/null || true)
  local resolved=""
  if command -v getent >/dev/null 2>&1; then
    resolved=$(getent ahosts "$host" 2>/dev/null | awk '{print $1}' | sort -u || true)
  fi
  local rip
  for ip in $ips; do
    if [[ "$ip" == "$host" ]]; then
      die "Адрес источника совпадает с адресом этого сервера. Перенос на самого себя остановлен."
    fi
    for rip in $resolved; do
      if [[ "$ip" == "$rip" ]]; then
        die "Адрес источника совпадает с адресом этого сервера. Перенос на самого себя остановлен."
      fi
    done
  done
}

build_ssh() {
  SSH_BASE=(ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0 -o ConnectTimeout=20 -o StrictHostKeyChecking=yes)
  SSH_BASE+=(-p "$SSH_PORT")
  if [[ -n "$SSH_KEY" ]]; then
    SSH_BASE+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
  fi
  SSH_BASE+=("${SSH_USER}@${SSH_HOST}")
}

ssh_exec() {
  "${SSH_BASE[@]}" "$1"
}

ask_ssh() {
  local host user port key
  read -r -p "Хост исходного сервера: " host
  read -r -p "Пользователь SSH [root]: " user
  user=${user:-root}
  read -r -p "Порт SSH [22]: " port
  port=${port:-22}
  read -r -p "Путь к ключу (пусто — ssh-agent): " key
  [[ "$host" =~ ^[A-Za-z0-9.:_-]+$ ]] || die "Хост содержит недопустимые символы."
  [[ "$user" =~ ^[A-Za-z_][A-Za-z0-9._-]{0,31}$ ]] || die "Имя пользователя SSH отклонено."
  [[ "$port" =~ ^[0-9]+$ ]] || die "Порт SSH отклонён."
  ((port >= 1 && port <= 65535)) || die "Порт SSH отклонён."
  if [[ -n "$key" ]]; then
    case "$key" in
      \~/*) key="${HOME}/${key:2}" ;;
    esac
    [[ "$key" != -* ]] || die "Путь к ключу не должен начинаться с дефиса."
    [[ -f "$key" ]] || die "Файл ключа не найден."
  fi
  refuse_if_local_target "$host"
  SSH_HOST=$host
  SSH_USER=$user
  SSH_PORT=$port
  SSH_KEY=$key
  build_ssh
}

remote_probe() {
  ssh_exec "bash -s" <<'REMOTE'
set -euo pipefail
umask 077

extract_key() {
  local key="$1" file="$2" line val
  [[ -f "$file" ]] || return 0
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1 || true)
  [[ -n "$line" ]] || return 0
  val=${line#*=}
  val=${val%$'\r'}
  if [[ ${#val} -ge 2 && ( "$val" == \"*\" || "$val" == \'*\' ) ]]; then
    val=${val:1:${#val}-2}
  fi
  printf '%s' "$val"
}

env_file=""
if [[ -f /etc/default/x-ui ]]; then
  env_file=/etc/default/x-ui
elif [[ -f /etc/conf.d/x-ui ]]; then
  env_file=/etc/conf.d/x-ui
elif [[ -f /etc/sysconfig/x-ui ]]; then
  env_file=/etc/sysconfig/x-ui
fi
if [[ -n "$env_file" ]]; then
  printf 'ENV_FILE=%s\n' "$env_file"
else
  printf 'ENV_FILE=none\n'
fi

db_type=""
db_folder="/etc/x-ui"
main_folder="/usr/local/x-ui"
if [[ -n "$env_file" ]]; then
  db_type=$(extract_key XUI_DB_TYPE "$env_file")
  folder_raw=$(extract_key XUI_DB_FOLDER "$env_file")
  main_raw=$(extract_key XUI_MAIN_FOLDER "$env_file")
  if [[ -n "$folder_raw" ]]; then
    db_folder=$folder_raw
  fi
  if [[ -n "$main_raw" ]]; then
    main_folder=$main_raw
  fi
fi

case "$db_type" in
  "" | sqlite | SQLite | SQLITE) printf 'DB_TYPE=sqlite\n' ;;
  postgres | postgresql | Postgres | PostgreSQL) printf 'DB_TYPE=postgres\n' ;;
  *) printf 'DB_TYPE=unsupported\n' ;;
esac

case "$db_folder" in
  *..*|*[!/A-Za-z0-9._-]*)
    printf 'DB_FOLDER_OK=no\n'
    db_folder=""
    ;;
  /*)
    printf 'DB_FOLDER=%s\n' "$db_folder"
    ;;
  *)
    printf 'DB_FOLDER_OK=no\n'
    db_folder=""
    ;;
esac

binary=/usr/local/x-ui/x-ui
case "$main_folder" in
  /*)
    case "$main_folder" in
      *..*|*[!/A-Za-z0-9._-]*) ;;
      *) binary="${main_folder%/}/x-ui" ;;
    esac
    ;;
esac
if [[ ! -x "$binary" && -x /usr/local/x-ui/x-ui ]]; then
  binary=/usr/local/x-ui/x-ui
fi
printf 'BINARY_OK=%s\n' "$([[ -x "$binary" ]] && printf yes || printf no)"

ver=""
if [[ -x "$binary" ]]; then
  ver=$("$binary" -v 2>/dev/null | head -n 1 | tr -d '[:space:]' || true)
fi
if [[ "$ver" =~ ^[A-Za-z0-9.+_-]+$ ]]; then
  printf 'VERSION=%s\n' "$ver"
else
  printf 'VERSION=invalid\n'
fi

marker=no
script_hits=()
[[ -f /usr/bin/x-ui ]] && script_hits+=(/usr/bin/x-ui)
[[ -f "${main_folder%/}/x-ui.sh" ]] && script_hits+=("${main_folder%/}/x-ui.sh")
[[ -f /usr/local/x-ui/x-ui.sh ]] && script_hits+=(/usr/local/x-ui/x-ui.sh)
if [[ ${#script_hits[@]} -gt 0 ]] && grep -E -i -q 'mhsanaei|3x-ui' "${script_hits[@]}" 2>/dev/null; then
  marker=yes
fi
if [[ "$marker" == no && -x "$binary" ]]; then
  hint=$("$binary" migrate-db 2>&1 || true)
  case "$hint" in
    *"nothing to do"*|*"--dump"*|*"--restore"*) marker=yes ;;
  esac
fi
printf 'MARKER=%s\n' "$marker"

mid=""
if [[ -r /etc/machine-id ]]; then
  mid=$(tr -d '[:space:]' </etc/machine-id || true)
fi
if [[ "$mid" =~ ^[a-f0-9]{32}$ ]]; then
  printf 'MACHINE_ID=%s\n' "$mid"
else
  printf 'MACHINE_ID=none\n'
fi

if [[ "$db_type" == postgres || "$db_type" == postgresql || "$db_type" == Postgres || "$db_type" == PostgreSQL ]]; then
  printf 'DB_EXISTS=no\n'
  exit 0
fi
case "$db_folder" in
  /*) ;;
  *)
    printf 'DB_EXISTS=no\n'
    exit 0
    ;;
esac
db="${db_folder%/}/x-ui.db"
if [[ ! -f "$db" ]]; then
  printf 'DB_EXISTS=no\n'
  exit 0
fi
printf 'DB_EXISTS=yes\n'

tmp=""
ok=0
cleanup() {
  if [[ "$ok" -ne 1 && -n "$tmp" ]]; then
    rm -f -- "$tmp"
  fi
}
trap cleanup EXIT

copied=no
if command -v sqlite3 >/dev/null 2>&1; then
  tmp=$(mktemp /tmp/xui-migrate.XXXXXX)
  chmod 600 "$tmp"
  if sqlite3 "$db" ".backup ${tmp}"; then
    printf 'COPY_KIND=sqlite\n'
    copied=yes
  else
    rm -f -- "$tmp"
    tmp=""
  fi
fi
if [[ "$copied" != yes ]]; then
  if [[ ! -x "$binary" ]]; then
    printf 'ERROR=copy\n'
    exit 1
  fi
  tmp=$(mktemp /tmp/xui-migrate.XXXXXX)
  chmod 600 "$tmp"
  if ! "$binary" migrate-db --src "$db" --dump "$tmp"; then
    printf 'ERROR=copy\n'
    exit 1
  fi
  printf 'COPY_KIND=dump\n'
fi
ok=1
printf 'COPY_PATH=%s\n' "$tmp"
REMOTE
}

parse_probe() {
  VERSION=""
  MARKER=""
  DB_TYPE=""
  DB_FOLDER=""
  DB_FOLDER_OK="yes"
  ENV_FILE=""
  DB_EXISTS=""
  COPY_KIND=""
  COPY_PATH=""
  BINARY_OK=""
  MACHINE_ID=""
  local line key val
  while IFS= read -r line; do
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    val=${line#*=}
    case "$key" in
      VERSION) VERSION=$val ;;
      MARKER) MARKER=$val ;;
      DB_TYPE) DB_TYPE=$val ;;
      DB_FOLDER) DB_FOLDER=$val ;;
      DB_FOLDER_OK) DB_FOLDER_OK=$val ;;
      ENV_FILE) ENV_FILE=$val ;;
      DB_EXISTS) DB_EXISTS=$val ;;
      COPY_KIND) COPY_KIND=$val ;;
      COPY_PATH) COPY_PATH=$val ;;
      BINARY_OK) BINARY_OK=$val ;;
      MACHINE_ID) MACHINE_ID=$val ;;
    esac
  done
}

accept_probe() {
  [[ "$BINARY_OK" == yes ]] || die "На источнике нет исполняемого бинарника 3x-ui. Панель источника не трогал."
  [[ "$VERSION" =~ ^[A-Za-z0-9.+_-]+$ ]] || die "Не удалось прочитать версию панели на источнике."
  [[ "$MARKER" == yes || "$MARKER" == no ]] || die "Ответ источника о типе панели непонятен."
  local rel
  rel=$(classify_release "$VERSION" "$MARKER")
  case "$rel" in
    ok\ v*)
      TAG=${rel#ok }
      ;;
    old)
      die "Источник не 3x-ui: похоже на старую панель x-ui. Прямой импорт её базы документация не поддерживает. Остановлено."
      ;;
    dev)
      die "Источник на dev-сборке. dev-latest этот скрипт не ставит и версию во время переноса не повышает. Остановлено."
      ;;
    too_old)
      die "Это 3x-ui старше v2.3.5. Официальный install.sh такую версию не ставит, а повышать версию во время переноса скрипт не будет. Остановлено."
      ;;
    *)
      die "Не удалось подтвердить, что источник — панель 3x-ui (MHSanaei). Остановлено."
      ;;
  esac
  [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Тег версии не принят."

  local kind
  kind=$(sanitize_db_type "$DB_TYPE")
  case "$kind" in
    postgres)
      die "На источнике PostgreSQL (XUI_DB_TYPE=postgres). Файла SQLite нет, подменять /etc/x-ui/x-ui.db нельзя. Официальный путь — x-ui migrate-db --dsn и переменные в env-файле панели. DSN с паролем не печатается."
      ;;
    sqlite) ;;
    *)
      die "Тип базы источника не SQLite. Само значение не показывается. Перенос остановлен."
      ;;
  esac
  [[ "$DB_FOLDER_OK" != no ]] || die "Каталог базы в env-файле источника не похож на безопасный абсолютный путь. Значение не показывается."
  [[ "$(db_folder_status "$DB_FOLDER")" == yes ]] || die "Каталог базы источника отклонён."
  [[ "$DB_EXISTS" == yes ]] || die "На источнике нет файла ${DB_FOLDER}/x-ui.db. Панель источника не останавливалась."
  case "$ENV_FILE" in
    /etc/default/x-ui | /etc/conf.d/x-ui | /etc/sysconfig/x-ui | none) ;;
    *) die "Имя env-файла источника отклонено." ;;
  esac
  case "$COPY_KIND" in
    sqlite | dump) ;;
    *) die "Согласованная копия базы не снята." ;;
  esac
  [[ "$COPY_PATH" =~ ^/tmp/xui-migrate\.[A-Za-z0-9]+$ ]] || die "Путь временной копии на источнике отклонён."
  local local_mid=""
  if [[ -r /etc/machine-id ]]; then
    local_mid=$(tr -d '[:space:]' </etc/machine-id || true)
  fi
  if [[ "$MACHINE_ID" =~ ^[a-f0-9]{32}$ && -n "$local_mid" && "$local_mid" == "$MACHINE_ID" ]]; then
    die "Источник — этот же сервер. Перенос на самого себя остановлен, база не подменялась."
  fi
}

pull_copy() {
  local q local_file magic
  REMOTE_TMP=$COPY_PATH
  q=$(printf '%q' "$COPY_PATH")
  local_file="${WORK}/download"
  if ! ssh_exec "cat -- ${q}" >"$local_file"; then
    die "Не удалось забрать копию базы. Исходная панель не останавливалась."
  fi
  if ! cleanup_remote; then
    die "Временная копия на источнике не удалена. На этом сервере панель не менялась."
  fi
  [[ -s "$local_file" ]] || die "Копия базы пустая."
  if [[ "$COPY_KIND" == sqlite ]]; then
    magic=$(head -c 15 -- "$local_file" || true)
    [[ "$magic" == "SQLite format 3" ]] || die "Копия не похожа на файл SQLite."
    cp -a -- "$local_file" "${WORK}/panel.db"
  else
    py load-dump "$local_file" "${WORK}/panel.db"
  fi
  chmod 600 "${WORK}/panel.db"
  rm -f -- "$local_file"
}

local_env_file() {
  local id=""
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    id=${ID:-}
  fi
  case "$id" in
    ubuntu | debian | armbian) printf '/etc/default/x-ui\n' ;;
    arch | manjaro | parch | alpine) printf '/etc/conf.d/x-ui\n' ;;
    *) printf '/etc/sysconfig/x-ui\n' ;;
  esac
}

prepare_local_env() {
  local folder=$1 file tmp
  file=$(local_env_file)
  mkdir -p -- "$(dirname -- "$file")"
  touch -- "$file"
  if grep -q -E '^(export[[:space:]]+)?XUI_DB_DSN=' "$file"; then
    warn "В env-файле панели был XUI_DB_DSN. Строка удаляется, значение не показывается. Эта миграция ставит SQLite."
  fi
  tmp=$(mktemp "${WORK}/env.XXXXXX")
  grep -v -E '^(export[[:space:]]+)?XUI_DB_(FOLDER|TYPE|DSN)=' "$file" >"$tmp" || true
  printf 'XUI_DB_TYPE=sqlite\n' >>"$tmp"
  if [[ "$folder" != /etc/x-ui ]]; then
    printf 'XUI_DB_FOLDER=%s\n' "$folder" >>"$tmp"
  fi
  cat "$tmp" >"$file"
  rm -f -- "$tmp"
  chmod 600 "$file" || true
  export XUI_DB_TYPE=sqlite
  unset XUI_DB_DSN || true
  if [[ "$folder" == /etc/x-ui ]]; then
    unset XUI_DB_FOLDER || true
  else
    export XUI_DB_FOLDER="$folder"
  fi
}

panel_active() {
  if command -v systemctl >/dev/null 2>&1 && systemctl cat x-ui >/dev/null 2>&1; then
    systemctl is-active --quiet x-ui
    return
  fi
  pgrep -f '/usr/local/x-ui/x-ui' >/dev/null 2>&1
}

stop_panel() {
  SYSTEMD_PAGER=cat PAGER=cat x-ui stop || true
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if ! panel_active; then
      break
    fi
    sleep 1
  done
  if panel_active; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl stop x-ui || true
      sleep 2
    fi
  fi
  if panel_active; then
    die "Панель на этом сервере не остановилась. Базу не подменяю."
  fi
}

start_panel() {
  SYSTEMD_PAGER=cat PAGER=cat x-ui start || true
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if panel_active; then
      break
    fi
    sleep 1
  done
  SYSTEMD_PAGER=cat PAGER=cat x-ui status || true
  panel_active || die "Панель не перешла в active после запуска."
}

port_listening() {
  local port=$1 dump
  if command -v ss >/dev/null 2>&1; then
    dump=$(ss -ltn || true)
    grep -E -q "[.:]${port}([^0-9]|$)" <<<"$dump"
    return
  fi
  python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket()
sock.settimeout(2)
try:
    sock.connect(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

read_panel_port() {
  local file=/etc/x-ui/install-result.env line val
  [[ -f "$file" ]] || return 0
  line=$(grep -E '^XUI_PANEL_PORT=' "$file" | tail -n 1 || true)
  [[ -n "$line" ]] || return 0
  val=${line#*=}
  val=${val#\'}
  val=${val%\'}
  val=${val#\"}
  val=${val%\"}
  [[ "$val" =~ ^[0-9]+$ ]] || return 0
  printf '%s\n' "$val"
}

current_ssh_port() {
  local port=22
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    port=${SSH_CONNECTION##* }
  fi
  if [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)); then
    printf '%s\n' "$port"
  else
    printf '22\n'
  fi
}

ask_nodes() {
  local flag
  flag=$(python3 - "${WORK}/inspect.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
nodes = report["nodes"]
if nodes["nonzero_node_id"] or nodes["origin_node_guid_ids"]:
    print("yes")
else:
    print("no")
PY
)
  if [[ "$flag" == yes ]]; then
    warn "Есть инбаунды с node_id или origin_node_guid. Локальный Xray на новом сервере может их не слушать."
    confirm_yes "Продолжить, не меняя nodes, node_id и origin_node_guid? Введите yes: " \
      || die "Остановлено. На этом сервере ничего не менял."
  fi
}

ask_addresses() {
  REPLACEMENTS=()
  info "Адреса ниже можно заменить точной строкой. SNI, dest, serverNames и stream_settings не меняются."
  local old new
  while true; do
    read -r -p "Старый адрес для замены (пусто — не менять больше ничего): " old || true
    [[ -n "$old" ]] || break
    read -r -p "Новый адрес: " new || true
    case "${old}${new}" in
      *$'\n'* | *$'\r'* | *$'\t'*)
        warn "Адрес с переводом строки не принимается."
        continue
        ;;
    esac
    if confirm_yes "Заменить «${old}» на «${new}» только в share_addr и hosts.address? Введите yes: "; then
      REPLACEMENTS+=("${old}"$'\t'"${new}")
    else
      info "Эта замена пропущена."
    fi
  done
}

run_strip() {
  local -a cmd
  cmd=(py strip "${WORK}/panel.db")
  local pair old new
  if ((${#REPLACEMENTS[@]})); then
    for pair in "${REPLACEMENTS[@]}"; do
      old=${pair%%$'\t'*}
      new=${pair#*$'\t'}
      cmd+=(--replace "$old" "$new")
    done
  fi
  "${cmd[@]}" >"${WORK}/strip.json"
  local -a verify_cmd
  verify_cmd=(py verify "${WORK}/panel.db" --baseline "${WORK}/inspect.json" --phase copy)
  if ((${#REPLACEMENTS[@]})); then
    verify_cmd+=(--allow share_addr --allow hosts.address)
  fi
  if ! "${verify_cmd[@]}" >"${WORK}/verify-copy.json"; then
    if [[ -s "${WORK}/verify-copy.json" ]]; then
      python3 - "${WORK}/verify-copy.json" <<'PY' >&2 || true
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
for item in report.get("errors") or []:
    print(item)
PY
    fi
    die "Очищенная копия не прошла проверку. На этом сервере ничего не менял."
  fi
}

copy_certs() {
  local db_path=$1 path required q status mode tmp
  while IFS=$'\t' read -r path required; do
    [[ -n "$path" ]] || continue
    case "$path" in
      /*) ;;
      *)
        warn "Пропущен неабсолютный путь сертификата."
        continue
        ;;
    esac
    if [[ "$path" == "$db_path" ]]; then
      warn "Путь сертификата совпадает с файлом базы, файл не копирую."
      continue
    fi
    q=$(printf '%q' "$path")
    status=$(ssh_exec "if [ -f ${q} ]; then echo FOUND; elif [ -e ${q} ]; then echo OTHER; else echo MISSING; fi" || true)
    if [[ "$status" != FOUND ]]; then
      if [[ "$required" == 1 ]]; then
        warn "Нет файла, на него ссылается включённый TLS-инбаунд: ${path}"
      else
        warn "Нет файла сертификата: ${path}"
      fi
      continue
    fi
    mode=$(safe_mode "$(ssh_exec "stat -c %a -- ${q}" || true)" 644)
    case "$path" in
      *[Kk]ey* | *priv* | *Priv*) mode=600 ;;
    esac
    mkdir -p -- "$(dirname -- "$path")"
    tmp=$(mktemp "${WORK}/cert.XXXXXX")
    if ! ssh_exec "cat -- ${q}" >"$tmp"; then
      warn "Не удалось прочитать сертификат: ${path}"
      rm -f -- "$tmp"
      continue
    fi
    install -o root -g root -m "$mode" -- "$tmp" "$path"
    rm -f -- "$tmp"
    info "Сертификат записан по прежнему пути: ${path}"
  done < <(py cert-paths "${WORK}/inspect.json")
}

place_database() {
  local src=$1 dest=$2 mode owner
  mode=600
  owner="0:0"
  mkdir -p -- "$(dirname -- "$dest")"
  if [[ -f "$dest" ]]; then
    mode=$(safe_mode "$(stat -c %a -- "$dest")" 600)
    owner=$(stat -c %u:%g -- "$dest")
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$dest" ".backup ${WORK}/replaced-before.db" \
        || cp -a -- "$dest" "${WORK}/replaced-before.db"
    else
      cp -a -- "$dest" "${WORK}/replaced-before.db"
      [[ -f "${dest}-wal" ]] && cp -a -- "${dest}-wal" "${WORK}/replaced-before.db-wal"
      [[ -f "${dest}-shm" ]] && cp -a -- "${dest}-shm" "${WORK}/replaced-before.db-shm"
    fi
  fi
  local staged="${dest}.migrate-new"
  if ! install -o root -g root -m "$mode" -- "$src" "$staged"; then
    rm -f -- "$staged"
    die "Не удалось записать новую базу. Прежний файл панели не заменялся."
  fi
  rm -f -- "${dest}-wal" "${dest}-shm" "${dest}-journal"
  mv -f -- "$staged" "$dest"
  chown "$owner" -- "$dest"
  chmod "$mode" -- "$dest"
}

apply_ufw_allows() {
  local line port proto
  while IFS= read -r line; do
    if [[ "$line" =~ ^ufw\ allow\ ([0-9]+)/(tcp|udp) ]]; then
      port=${BASH_REMATCH[1]}
      proto=${BASH_REMATCH[2]}
      ufw allow "${port}/${proto}" || warn "Не удалось добавить правило ${port}/${proto}."
    fi
  done
}

ask_firewall() {
  local ssh_port
  ssh_port=$(current_ssh_port)
  info "Команды файрвола по фактическим портам. Скрипт их сам не включает."
  py firewall "${WORK}/inspect.json" --ssh-port "$ssh_port"
  info "Строка ufw enable и файл nftables выше не выполняются: default-deny может оборвать эту SSH-сессию."
  if ! confirm_yes "Применить только ufw allow для этих портов, без ufw enable? Введите yes: "; then
    info "Порты не открывал."
    return 0
  fi
  if ! command -v ufw >/dev/null 2>&1; then
    warn "ufw не установлен. Ставить брандмауэр и включать его скрипт не будет."
    return 0
  fi
  py firewall "${WORK}/inspect.json" --ssh-port "$ssh_port" | apply_ufw_allows
  info "Разрешающие правила ufw записаны. ufw enable не запускался."
}

print_final_numbers() {
  python3 - "${WORK}/inspect.json" "${WORK}/verify-live.json" <<'PY'
import json, sys
before = json.load(open(sys.argv[1], encoding="utf-8"))
after = json.load(open(sys.argv[2], encoding="utf-8"))
rows = after["client_rows"]

def cell(name):
    value = rows.get(name)
    return "нет таблицы" if value is None else str(value)

print("инбаунды до: %s, после: %s" % (before["inbound_count"], after["inbound_count"]))
print("reality до: %s, после: %s, поля на месте: %s" % (
    before["reality_count"], after["reality_count"], after["reality_fields_ok"]))
print("clients: %s" % cell("clients"))
print("client_traffics: %s" % cell("client_traffics"))
print("webPort: %s" % after["web_port"])
print("пользователи панели: %s" % (", ".join(after["usernames"]) or "нет"))
if not after["ok"]:
    print("проверка базы не сошлась:")
    for item in after["errors"]:
        print("  " + item)
PY
}

show_plan() {
  cat <<EOF
План на этом сервере:
  Поставить ту же версию официальным скриптом: ${TAG}
  Остановить x-ui, подменить ${DB_FOLDER}/x-ui.db очищенной копией.
  Вернуть сертификаты по тем же абсолютным путям и запустить панель.
  Исходный ${SSH_USER}@${SSH_HOST} не останавливается и не удаляется.
  Клиенты, UUID и клиентский трафик из копии убираются.
  protocol, port, Reality (privateKey, shortIds, serverNames, dest), gRPC serviceName и прочий stream_settings не пересобираются.
  Логин после подмены — пользователь из таблицы users, не пароль из install-result.env.
EOF
  if ((${#REPLACEMENTS[@]})); then
    info "Подтверждённые замены адресов записаны только для share_addr и hosts.address."
  else
    info "share_addr и hosts.address пока не менялись."
  fi
}

migrate() {
  ask_ssh
  info "Читаю источник. Панель на нём не останавливаю."
  local probe_out
  local ssh_err="${WORK}/ssh.err"
  if ! probe_out=$(remote_probe 2>"$ssh_err"); then
    if [[ -s "$ssh_err" ]] && grep -q 'Host key verification failed' "$ssh_err"; then
      die "Ключ хоста источника не найден в known_hosts. Сначала один раз зайдите по SSH и проверьте отпечаток. Пароль в этот скрипт не передаётся."
    fi
    if grep -q '^ERROR=copy' <<<"$probe_out"; then
      die "Согласованную копию базы снять не удалось. На источнике нужен sqlite3 либо x-ui с командой migrate-db --dump. Панель источника не останавливалась."
    fi
    die "SSH к источнику не удался. Нужен ключ или ssh-agent. Пароль в аргументы не передаётся."
  fi
  parse_probe <<<"$probe_out"
  if [[ -n "$COPY_PATH" ]]; then
    REMOTE_TMP=$COPY_PATH
  fi
  accept_probe
  info "Источник: 3x-ui ${TAG}, SQLite ${DB_FOLDER}/x-ui.db, env ${ENV_FILE}."
  pull_copy
  py inspect "${WORK}/panel.db" --out "${WORK}/inspect.json"
  py summary "${WORK}/inspect.json"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    REPLACEMENTS=()
    run_strip
    show_plan
    info "dry-run: панель не ставилась, база на этом сервере не подменялась."
    SUCCESS=1
    return 0
  fi
  ask_nodes
  ask_addresses
  run_strip
  show_plan
  confirm_yes "Продолжить изменения на ЭТОМ сервере? Введите yes: " \
    || die "Отменено. На этом сервере панель не ставилась и база не подменялась."

  local dest count
  dest="${DB_FOLDER}/x-ui.db"
  if [[ -e "$dest" && ! -f "$dest" ]]; then
    die "${dest} существует и это не файл. Ничего больше не меняю."
  fi
  count=0
  if [[ -f "$dest" ]]; then
    count=$(py inbound-count "$dest")
    info "На этом сервере уже есть ${dest}, инбаундов: ${count}."
  fi
  if [[ "$count" -gt 0 ]]; then
    confirm_yes "Заменить эту непустую базу очищенной копией? Введите yes: " \
      || die "Отменено. Существующую базу не затирал."
    cp -a -- "$dest" "${WORK}/pre-existing.db"
  fi

  local env_file env_existed=0
  env_file=$(local_env_file)
  if [[ -f "$env_file" ]]; then
    env_existed=1
    cp -a -- "$env_file" "${WORK}/env.before"
  fi
  prepare_local_env "$DB_FOLDER"
  info "Ставлю ${TAG}. dev-latest не используется."
  if ! run_official_installer "$TAG"; then
    if [[ "$env_existed" -eq 1 ]]; then
      cp -a -- "${WORK}/env.before" "$env_file"
    else
      rm -f -- "$env_file"
    fi
    die "Официальный установщик не завершился. Базу не подменял и панель не останавливал."
  fi
  prepare_local_env "$DB_FOLDER"
  if [[ "$DB_FOLDER" != /etc/x-ui ]] && command -v systemctl >/dev/null 2>&1; then
    if ! systemctl cat x-ui 2>/dev/null | grep -F -q -- "$DB_FOLDER"; then
      warn "Каталог ${DB_FOLDER} не попал в unit x-ui. Под ProtectSystem панель может не открыть базу."
    fi
  fi
  if [[ -f /etc/x-ui/install-result.env ]]; then
    info "Случайные данные свежей установки лежат в /etc/x-ui/install-result.env."
    info "Пароль оттуда повторно не печатаю: после подмены базы он не подойдёт."
  fi

  stop_panel
  place_database "${WORK}/panel.db" "$dest"
  copy_certs "$dest"
  start_panel

  local -a live_cmd fresh
  live_cmd=(py verify "$dest" --baseline "${WORK}/inspect.json" --phase live)
  if ((${#REPLACEMENTS[@]})); then
    live_cmd+=(--allow share_addr --allow hosts.address)
  fi
  set +e
  "${live_cmd[@]}" >"${WORK}/verify-live.json"
  local verify_status=$?
  set -e
  if panel_active; then
    info "Сервис x-ui: active"
  else
    warn "Сервис x-ui не active."
  fi
  print_final_numbers || true
  local web_port=""
  if [[ -s "${WORK}/verify-live.json" ]]; then
    web_port=$(python3 - "${WORK}/verify-live.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
value = report.get("web_port")
print(value if isinstance(value, int) else "")
PY
) || web_port=""
  fi
  fresh=$(read_panel_port || true)
  if [[ -n "$fresh" && -n "$web_port" && "$fresh" != "$web_port" ]]; then
    info "Порт свежей установки был ${fresh}. После подмены базы панель должна слушать ${web_port} из settings."
  fi
  if [[ -n "$web_port" ]]; then
    local i heard=0
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      if port_listening "$web_port"; then
        heard=1
        break
      fi
      sleep 1
    done
    if [[ "$heard" -eq 1 ]]; then
      info "Порт панели ${web_port} слушается."
    else
      warn "Порт ${web_port} из settings не слушался."
      verify_status=1
    fi
  else
    warn "В базе нет webPort, сверить порт панели нельзя."
    verify_status=1
  fi
  info "Вход в панель — пользователь из таблицы users, пароль тот же, что на старом сервере."
  info "Хеш пароля не печатается. Учётные данные из /etc/x-ui/install-result.env после подмены базы не подходят."
  info "Исходный сервер не останавливался и 3x-ui на нём не удалялся."
  ask_firewall
  [[ "$verify_status" -eq 0 ]] || die "Проверка после запуска не сошлась. Панель оставлена запущенной, исходный сервер не трогался."
  SUCCESS=1
}

run_official_installer() {
  local tag=${1:-} script log status=0 kept
  command -v curl >/dev/null 2>&1 || {
    warn "Нужен curl, чтобы скачать официальный install.sh."
    return 1
  }
  script="${WORK}/install.sh"
  log="${WORK}/install.log"
  if ! curl -fsSL --retry 3 --connect-timeout 20 -o "$script" \
    https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh; then
    warn "Не удалось скачать официальный install.sh."
    return 1
  fi
  if [[ ! -s "$script" ]]; then
    warn "Скачанный install.sh пустой."
    return 1
  fi
  if ! grep -q 'XUI_NONINTERACTIVE' "$script"; then
    warn "Скачанный файл не похож на официальный install.sh."
    return 1
  fi
  chmod 700 "$script"
  if [[ -n "$tag" ]]; then
    XUI_NONINTERACTIVE=1 bash "$script" "$tag" >"$log" 2>&1 || status=$?
  else
    XUI_NONINTERACTIVE=1 bash "$script" >"$log" 2>&1 || status=$?
  fi
  if [[ "$status" -ne 0 ]]; then
    kept=/root/xui-install-failed.log
    cp -a -- "$log" "$kept"
    chmod 600 "$kept"
    warn "Журнал установщика: ${kept}. В нём может быть пароль свежей установки."
    return 1
  fi
  rm -f -- "$log" "$script"
  return 0
}

clean_install() {
  info "Чистая установка. dev-latest не используется."
  info "Скрипт скачает https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh и запустит его с XUI_NONINTERACTIVE=1."
  info "Журнал установщика на экран не выводится: в нём бывает пароль."
  info "Учётные данные установщик запишет в /etc/x-ui/install-result.env."
  confirm_yes "Начать чистую установку на этом сервере? Введите yes: " || die "Отменено."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "dry-run: установка не запускалась."
    return 0
  fi
  run_official_installer || die "Официальный установщик не завершился."
  if [[ -f /etc/x-ui/install-result.env ]]; then
    info "Учётные данные лежат в /etc/x-ui/install-result.env."
    info "Пароль из этого файла в лог повторно не выводится."
  else
    warn "Файл /etc/x-ui/install-result.env не появился."
  fi
}

main() {
  local arg
  while [[ $# -gt 0 ]]; do
    arg=$1
    shift
    case "$arg" in
      --dry-run) DRY_RUN=1 ;;
      -h | --help)
        usage
        exit 0
        ;;
      *) die "Неизвестный аргумент. Смотрите --help." ;;
    esac
  done
  require_root
  require_tty
  require_python
  take_lock
  trap on_exit EXIT
  WORK=$(mktemp -d /root/xui-migrate.XXXXXX)
  chmod 700 "$WORK"
  info "Режим миграции панели 3x-ui."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "Включён --dry-run: установка и подмена базы выполняться не будут."
  fi
  info "1) Чистая установка"
  info "2) Миграция с другого сервера"
  info "0) Выход"
  local choice
  read -r -p "Режим: " choice
  case "$choice" in
    1) clean_install ;;
    2) migrate ;;
    0) exit 0 ;;
    *) die "Нет такого режима." ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
