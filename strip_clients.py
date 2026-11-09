#!/usr/bin/env python3
"""Правка копии SQLite 3x-ui перед первым запуском на новом сервере.

Скрипт не ставит панель и не ходит в REST API. Он правит уже снятую копию
базы: опустошает клиентов и в таблице, и в JSON settings, потому что сидер
ClientsTable поднимает клиентов из settings, а GenXrayInboundConfig отдаёт
в Xray именно этот JSON. history_of_seeders и схема не трогаются.
Недостающие таблицы пропускаются.

Запускать против копии, не против живой базы источника.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

CLIENT_TABLES = (
    "clients",
    "client_inbounds",
    "client_traffics",
    "inbound_client_ips",
    "client_hwids",
    "client_external_links",
    "client_groups",
    # Появились позже основного списка: почта клиента и его трафик.
    "node_client_traffics",
    "client_global_traffics",
)

SETTING_CERT_KEYS = ("webCertFile", "webKeyFile", "subCertFile", "subKeyFile")

# up/down обнуляются, settings переписывается. Остальные колонки очистка
# не меняет. share_addr и hosts.address — только после отдельного yes.
SKIP_HASH_COLUMNS = {"settings", "up", "down"}
PRESERVE_COLUMNS = {
    "protocol",
    "port",
    "listen",
    "remark",
    "enable",
    "tag",
    "expiry_time",
    "total",
    "traffic_reset",
    "sniffing",
    "stream_settings",
    "share_addr",
    "share_addr_strategy",
    "disable_flow",
    "sub_sort_index",
    "exclude_from_sub",
    "node_id",
    "origin_node_guid",
}

UDP_PROTOCOLS = {"wireguard", "amneziawg", "hysteria", "tuic", "masque"}
UDP_NETWORKS = {"kcp", "mkcp", "quic", "udp"}
TCP_NETWORKS = {
    "tcp",
    "ws",
    "websocket",
    "http",
    "h2",
    "grpc",
    "httpupgrade",
    "xhttp",
    "splithttp",
}

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationError(Exception):
    def __init__(self, message: str, inbound_id=None):
        self.inbound_id = inbound_id
        super().__init__(message)


def qident(name: str) -> str:
    if not IDENT_RE.fullmatch(name or ""):
        raise MigrationError("небезопасное имя таблицы или колонки")
    return '"' + name + '"'


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = None
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=5000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def column_names(conn: sqlite3.Connection, table: str) -> list:
    rows = conn.execute("PRAGMA table_info(" + qident(table) + ")").fetchall()
    return [row[1] for row in rows]


def fetch_rows(conn: sqlite3.Connection, table: str):
    if not table_exists(conn, table):
        return None
    cols = column_names(conn, table)
    if not cols:
        return []
    sql = "SELECT " + ", ".join(qident(c) for c in cols) + " FROM " + qident(table)
    out = []
    for rec in conn.execute(sql):
        out.append(dict(zip(cols, rec)))
    return out


def canon_bytes(value) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"f" + repr(value).encode("ascii")
    if isinstance(value, bytes):
        return b"B" + value
    return b"s" + str(value).encode("utf-8")


def sha256_text(value) -> str:
    return hashlib.sha256(canon_bytes(value)).hexdigest()


def parse_json_object(raw, inbound_id, what: str):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(
                "инбаунд id=%s: %s не текст" % (inbound_id, what),
                inbound_id,
            ) from exc
    if not isinstance(raw, str):
        raise MigrationError(
            "инбаунд id=%s: %s неожиданного типа" % (inbound_id, what),
            inbound_id,
        )
    if raw.strip() == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MigrationError(
            "инбаунд id=%s: %s не разбирается как JSON (%s)"
            % (inbound_id, what, exc.msg),
            inbound_id,
        ) from exc


def transform_settings(raw, inbound_id: int) -> str:
    """Вернуть settings с пустыми clients и peers, остальные поля сохранив.

    Пустая строка и NULL становятся {"clients":[]}. Битый JSON и не-объект
    останавливают миграцию: исходный текст вызывающий код ещё не записывал.
    """
    parsed = parse_json_object(raw, inbound_id, "settings")
    if parsed is None:
        data = {}
    elif not isinstance(parsed, dict):
        raise MigrationError(
            "инбаунд id=%s: settings не объект JSON" % inbound_id,
            inbound_id,
        )
    else:
        data = parsed

    # Ключа нет, null или не массив — всё равно пустой список.
    # Уже массив тоже заменяется: старых клиентов остаться не должно.
    data["clients"] = []

    if "peers" in data:
        peers = data["peers"]
        if peers is not None and not isinstance(peers, list):
            raise MigrationError(
                "инбаунд id=%s: peers не массив и не null, settings не изменён"
                % inbound_id,
                inbound_id,
            )
        data["peers"] = []

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def apply_replacements(value, pairs):
    if not pairs or not isinstance(value, str):
        return value, False
    current = value
    changed = False
    for old, new in pairs:
        if current == old:
            current = new
            changed = True
    return current, changed


def validate_address(value: str, label: str) -> str:
    if not isinstance(value, str) or value == "" or value != value.strip():
        raise MigrationError("%s не похож на IP или домен" % label)
    if value.startswith("-") or len(value) > 253:
        raise MigrationError("%s не похож на IP или домен" % label)
    if any(ord(char) < 33 or char in "$`'\"\\;|&<>" for char in value):
        raise MigrationError("%s не похож на IP или домен" % label)
    return value


def is_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return False


def node_id_set(value) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip()
        return text not in {"", "0"}
    return True


def load_stream(raw, inbound_id):
    parsed = parse_json_object(raw, inbound_id, "stream_settings")
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise MigrationError(
            "инбаунд id=%s: stream_settings не объект JSON" % inbound_id,
            inbound_id,
        )
    return parsed


def reality_problems(stream: dict) -> list:
    security = stream.get("security")
    if not isinstance(security, str) or security.lower() != "reality":
        return []
    reality = stream.get("realitySettings")
    if not isinstance(reality, dict):
        return ["realitySettings"]
    problems = []
    private_key = reality.get("privateKey")
    if not isinstance(private_key, str) or private_key.strip() == "":
        problems.append("privateKey")
    dest = reality.get("dest")
    if not isinstance(dest, str) or dest.strip() == "":
        problems.append("dest")
    names = reality.get("serverNames")
    if (
        not isinstance(names, list)
        or not any(isinstance(item, str) and item.strip() for item in names)
    ):
        problems.append("serverNames")
    short_ids = reality.get("shortIds")
    if (
        not isinstance(short_ids, list)
        or len(short_ids) == 0
        or not all(isinstance(item, str) for item in short_ids)
    ):
        problems.append("shortIds")
    return problems


def cert_paths_from_stream(stream: dict) -> list:
    tls = stream.get("tlsSettings")
    if not isinstance(tls, dict):
        return []
    certs = tls.get("certificates")
    if not isinstance(certs, list):
        return []
    found = []
    for item in certs:
        if not isinstance(item, dict):
            continue
        for key in ("certificateFile", "keyFile"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
    return found


def inline_tls(stream: dict) -> bool:
    tls = stream.get("tlsSettings")
    if not isinstance(tls, dict):
        return False
    certs = tls.get("certificates")
    if not isinstance(certs, list):
        return False
    for item in certs:
        if not isinstance(item, dict):
            continue
        for key in ("certificate", "key"):
            value = item.get(key)
            if isinstance(value, str) and ("BEGIN" in value or len(value) > 80):
                return True
    return False


def classify_path(value: str) -> str:
    if "BEGIN" in value or "\n" in value or "\x00" in value or "\t" in value:
        return "inline"
    if not value.startswith("/"):
        return "relative"
    if ".." in value.split("/"):
        return "unsafe"
    return "absolute"


def l4_for(protocol, stream: dict, settings) -> list:
    networks = set()
    network = stream.get("network") if isinstance(stream, dict) else None
    if isinstance(network, str) and network.strip():
        networks.add(network.strip().lower())
    if isinstance(settings, dict):
        extra = settings.get("network")
        if isinstance(extra, str):
            for part in extra.split(","):
                if part.strip():
                    networks.add(part.strip().lower())
    l4 = set()
    proto = (protocol or "")
    if isinstance(proto, str) and proto.lower() in UDP_PROTOCOLS:
        l4.add("udp")
    if networks & UDP_NETWORKS:
        l4.add("udp")
    if networks & TCP_NETWORKS:
        l4.add("tcp")
    if not l4:
        l4.add("tcp")
    return sorted(l4)


def setting_map(conn: sqlite3.Connection) -> dict:
    rows = fetch_rows(conn, "settings")
    if not rows:
        return {}
    found = {}
    for row in rows:
        key = row.get("key")
        if isinstance(key, str):
            found[key] = row.get("value")
    return found


def bool_setting(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return is_enabled(value)


def int_setting(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def hash_columns(row: dict, skip: set) -> dict:
    hashed = {}
    for key, value in row.items():
        if key in skip or key == "id":
            continue
        hashed[key] = sha256_text(value)
    return hashed


def count_json_clients(settings) -> int:
    if not isinstance(settings, dict):
        return 0
    clients = settings.get("clients")
    if isinstance(clients, list):
        return len(clients)
    return 0


def build_report(conn: sqlite3.Connection) -> dict:
    if not table_exists(conn, "inbounds"):
        raise MigrationError("в базе нет таблицы inbounds — это не база 3x-ui")
    inbound_cols = set(column_names(conn, "inbounds"))
    if "settings" not in inbound_cols:
        raise MigrationError("в inbounds нет колонки settings")
    if "stream_settings" not in inbound_cols:
        raise MigrationError("в inbounds нет колонки stream_settings")

    inbounds = fetch_rows(conn, "inbounds") or []
    public = []
    baseline_in = {}
    addresses = []
    certs = {}
    json_clients = 0
    json_peers = 0
    reality_ids = []
    reality_bad = []
    inline_tls_ids = []

    for row in inbounds:
        inbound_id = row.get("id")
        settings = parse_json_object(row.get("settings"), inbound_id, "settings")
        if settings is None:
            settings = {}
        elif not isinstance(settings, dict):
            raise MigrationError(
                "инбаунд id=%s: settings не объект JSON" % inbound_id,
                inbound_id,
            )
        stream = load_stream(row.get("stream_settings"), inbound_id)
        json_clients += count_json_clients(settings)
        peers = settings.get("peers")
        if isinstance(peers, list):
            json_peers += len(peers)
        # Та же проверка, что и перед записью: битый JSON останавливает отчёт.
        transform_settings(row.get("settings"), inbound_id)
        problems = reality_problems(stream)
        security = stream.get("security") if isinstance(stream.get("security"), str) else ""
        if security.lower() == "reality":
            reality_ids.append(inbound_id)
            if problems:
                reality_bad.append({"id": inbound_id, "missing": problems})
        enabled = is_enabled(row.get("enable")) if "enable" in row else True
        if security.lower() == "tls":
            if inline_tls(stream):
                inline_tls_ids.append(inbound_id)
            for path in cert_paths_from_stream(stream):
                add_cert(certs, path, "inbound:%s" % inbound_id, enabled)
        protocol = row.get("protocol")
        port = row.get("port")
        public.append(
            {
                "id": inbound_id,
                "protocol": protocol if isinstance(protocol, str) else "",
                "port": port if isinstance(port, int) else None,
                "network": stream.get("network") if isinstance(stream.get("network"), str) else "",
                "security": security,
                "enable": enabled,
                "l4": l4_for(protocol, stream, settings),
                "reality_ok": security.lower() == "reality" and not problems,
            }
        )
        if "share_addr" in row and isinstance(row.get("share_addr"), str) and row["share_addr"].strip():
            addresses.append(
                {
                    "field": "inbounds.share_addr",
                    "id": inbound_id,
                    "value": row["share_addr"],
                }
            )
        baseline_in[str(inbound_id)] = hash_columns(row, SKIP_HASH_COLUMNS)

    settings = setting_map(conn)
    for key in SETTING_CERT_KEYS:
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            add_cert(certs, value.strip(), key, False)

    hosts = fetch_rows(conn, "hosts")
    baseline_hosts = {}
    if hosts is not None:
        for row in hosts:
            host_id = row.get("id")
            baseline_hosts[str(host_id)] = hash_columns(row, set())
            if isinstance(row.get("address"), str) and row["address"].strip():
                addresses.append(
                    {
                        "field": "hosts.address",
                        "id": host_id,
                        "value": row["address"],
                    }
                )

    client_rows = {}
    for name in CLIENT_TABLES:
        rows = fetch_rows(conn, name)
        client_rows[name] = None if rows is None else len(rows)

    users = []
    for row in fetch_rows(conn, "users") or []:
        name = row.get("username")
        if isinstance(name, str) and name:
            users.append(name)

    nodes = node_report(conn, inbounds)
    bots = {
        "telegram_enabled": bool_setting(settings.get("tgBotEnable")),
        "telegram_token_set": bool(isinstance(settings.get("tgBotToken"), str) and settings.get("tgBotToken").strip()),
        "discord_enabled": bool_setting(settings.get("discordBotEnable")),
        "discord_token_set": bool(
            isinstance(settings.get("discordBotToken"), str) and settings.get("discordBotToken").strip()
        ),
    }
    api_rows = fetch_rows(conn, "api_tokens")
    unknown_traffic = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ):
        if "traffic" in name and name not in CLIENT_TABLES and name != "outbound_traffics":
            unknown_traffic.append(name)

    return {
        "inbound_count": len(inbounds),
        "reality_count": len(reality_ids),
        "reality_bad": reality_bad,
        "json_clients": json_clients,
        "json_peers": json_peers,
        "client_rows": client_rows,
        "usernames": users,
        "web_port": int_setting(settings.get("webPort")),
        "sub_port": int_setting(settings.get("subPort")),
        "sub_enable": bool_setting(settings.get("subEnable")) if "subEnable" in settings else None,
        "two_factor_enabled": bool_setting(settings.get("twoFactorEnable")),
        "inbounds": public,
        "addresses": addresses,
        "certs": list(certs.values()),
        "inline_tls_ids": inline_tls_ids,
        "nodes": nodes,
        "bots": bots,
        "api_token_rows": None if api_rows is None else len(api_rows),
        "unknown_traffic_tables": unknown_traffic,
        "baseline": {"inbounds": baseline_in, "hosts": baseline_hosts},
    }


def add_cert(certs: dict, path: str, source: str, enabled_tls: bool) -> None:
    kind = classify_path(path)
    item = certs.get(path)
    if item is None:
        item = {
            "path": path if kind == "absolute" else "",
            "kind": kind,
            "sources": [],
            "enabled_tls": False,
        }
        certs[path] = item
    if source not in item["sources"]:
        item["sources"].append(source)
    if enabled_tls:
        item["enabled_tls"] = True
    if kind != "absolute":
        # Содержимое PEM и относительный мусор в отчёт не кладём.
        item["path"] = ""


def node_report(conn: sqlite3.Connection, inbounds: list) -> dict:
    nonzero = []
    origins = []
    for row in inbounds:
        if "node_id" in row and node_id_set(row.get("node_id")):
            nonzero.append(row.get("id"))
        origin = row.get("origin_node_guid") if "origin_node_guid" in row else None
        if isinstance(origin, str) and origin.strip():
            origins.append(row.get("id"))
    names = []
    node_rows = fetch_rows(conn, "nodes")
    count = None if node_rows is None else len(node_rows)
    if node_rows:
        for row in node_rows:
            # api_token колонка есть в модели, в отчёт её не берём.
            names.append(
                {
                    "name": row.get("name") if isinstance(row.get("name"), str) else "",
                    "address": row.get("address") if isinstance(row.get("address"), str) else "",
                    "port": row.get("port") if isinstance(row.get("port"), int) else None,
                }
            )
    return {
        "table_rows": count,
        "nonzero_node_id": nonzero,
        "origin_node_guid_ids": origins,
        "listed": names,
    }


def assert_clean_settings(raw, inbound_id) -> None:
    parsed = parse_json_object(raw, inbound_id, "settings")
    if not isinstance(parsed, dict):
        raise MigrationError(
            "инбаунд id=%s: после очистки settings не объект" % inbound_id,
            inbound_id,
        )
    if parsed.get("clients") != []:
        raise MigrationError(
            "инбаунд id=%s: clients не пустой массив" % inbound_id,
            inbound_id,
        )
    if "peers" in parsed and parsed.get("peers") != []:
        raise MigrationError(
            "инбаунд id=%s: peers не пустой массив" % inbound_id,
            inbound_id,
        )


def strip_database(path: str, replacements) -> dict:
    for old, new in replacements:
        validate_address(old, "старый адрес")
        validate_address(new, "новый адрес")
    conn = connect(path, readonly=False)
    try:
        report_before = build_report(conn)
        inbounds = fetch_rows(conn, "inbounds") or []
        inbound_cols = set(column_names(conn, "inbounds"))
        planned = []
        replaced_share = 0
        for row in inbounds:
            inbound_id = row.get("id")
            new_settings = transform_settings(row.get("settings"), inbound_id)
            share_sql = None
            if "share_addr" in inbound_cols:
                updated, changed = apply_replacements(row.get("share_addr"), replacements)
                if changed:
                    share_sql = updated
                    replaced_share += 1
            planned.append((inbound_id, new_settings, share_sql))

        host_plans = []
        if table_exists(conn, "hosts") and "address" in set(column_names(conn, "hosts")):
            for row in fetch_rows(conn, "hosts") or []:
                updated, changed = apply_replacements(row.get("address"), replacements)
                if changed:
                    host_plans.append((row.get("id"), updated))

        conn.execute("BEGIN IMMEDIATE")
        try:
            for inbound_id, new_settings, share_sql in planned:
                sets = [qident("settings") + " = ?"]
                params = [new_settings]
                if "up" in inbound_cols:
                    sets.append(qident("up") + " = 0")
                if "down" in inbound_cols:
                    sets.append(qident("down") + " = 0")
                if share_sql is not None:
                    sets.append(qident("share_addr") + " = ?")
                    params.append(share_sql)
                params.append(inbound_id)
                conn.execute(
                    "UPDATE "
                    + qident("inbounds")
                    + " SET "
                    + ", ".join(sets)
                    + " WHERE "
                    + qident("id")
                    + " = ?",
                    params,
                )
            for host_id, new_addr in host_plans:
                conn.execute(
                    "UPDATE " + qident("hosts") + " SET " + qident("address") + " = ? WHERE " + qident("id") + " = ?",
                    (new_addr, host_id),
                )
            deleted = {}
            for name in CLIENT_TABLES:
                if table_exists(conn, name):
                    before = conn.execute("SELECT COUNT(*) FROM " + qident(name)).fetchone()[0]
                    conn.execute("DELETE FROM " + qident(name))
                    deleted[name] = before
                else:
                    deleted[name] = None
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        for inbound_id, new_settings, _share in planned:
            assert_clean_settings(new_settings, inbound_id)
        return {
            "inbound_count": report_before["inbound_count"],
            "replaced_share_addr": replaced_share,
            "replaced_hosts": len(host_plans),
            "deleted": deleted,
        }
    finally:
        conn.close()


def verify_database(path: str, baseline: dict, allow, phase: str = "copy") -> dict:
    conn = connect(path, readonly=True)
    try:
        report = build_report(conn)
        errors = []
        before_in = baseline.get("inbounds") or {}
        if report["inbound_count"] != len(before_in):
            errors.append(
                "число инбаундов %s, в снимке %s"
                % (report["inbound_count"], len(before_in))
            )
        if report["reality_bad"]:
            for item in report["reality_bad"]:
                errors.append(
                    "инбаунд id=%s: reality без %s"
                    % (item["id"], ", ".join(item["missing"]))
                )
        current = fetch_rows(conn, "inbounds") or []
        by_id = {str(row.get("id")): row for row in current}
        if set(by_id) != set(before_in):
            errors.append("набор id инбаундов изменился")
        allow = set(allow or [])
        for inbound_id, hashes in before_in.items():
            row = by_id.get(inbound_id)
            if row is None:
                continue
            try:
                assert_clean_settings(row.get("settings"), inbound_id)
            except MigrationError as exc:
                errors.append(str(exc))
            # После старта панель сама может писать счётчики. Ноль проверяем
            # на копии до запуска.
            if phase == "copy":
                if "up" in row and row.get("up") not in (0, None):
                    errors.append("инбаунд id=%s: up не обнулён" % inbound_id)
                if "down" in row and row.get("down") not in (0, None):
                    errors.append("инбаунд id=%s: down не обнулён" % inbound_id)
            for column, digest in hashes.items():
                if phase == "live" and column not in PRESERVE_COLUMNS:
                    continue
                if column == "share_addr" and "share_addr" in allow:
                    continue
                if sha256_text(row.get(column)) != digest:
                    errors.append(
                        "инбаунд id=%s: колонка %s изменилась" % (inbound_id, column)
                    )
        before_hosts = baseline.get("hosts") or {}
        hosts = fetch_rows(conn, "hosts")
        host_map = {}
        if hosts is not None:
            host_map = {str(row.get("id")): row for row in hosts}
        if phase == "copy" and set(host_map) != set(before_hosts):
            errors.append("набор hosts изменился")
        for host_id, hashes in before_hosts.items():
            row = host_map.get(host_id)
            if row is None:
                if phase == "copy":
                    errors.append("host id=%s пропал" % host_id)
                continue
            for column, digest in hashes.items():
                if phase == "live" and column != "address":
                    continue
                if column == "address" and "hosts.address" in allow:
                    continue
                if sha256_text(row.get(column)) != digest:
                    errors.append("host id=%s: колонка %s изменилась" % (host_id, column))
        for name, count in report["client_rows"].items():
            if count not in (None, 0):
                errors.append("таблица %s не пуста (%s)" % (name, count))
        web_before = baseline.get("web_port")
        if web_before is not None and report["web_port"] != web_before:
            errors.append(
                "webPort в базе %s, до подмены был %s" % (report["web_port"], web_before)
            )
        reality_now = sum(1 for item in report["inbounds"] if item["security"].lower() == "reality")
        if "reality_count" in baseline and reality_now != baseline["reality_count"]:
            errors.append(
                "reality-инбаундов %s, было %s" % (reality_now, baseline["reality_count"])
            )
        result = {
            "ok": not errors,
            "errors": errors,
            "inbound_count": report["inbound_count"],
            "reality_count": reality_now,
            "reality_fields_ok": sum(1 for item in report["inbounds"] if item["reality_ok"]),
            "clients_json_empty": report["inbound_count"],
            "client_rows": report["client_rows"],
            "web_port": report["web_port"],
            "sub_port": report["sub_port"],
            "usernames": report["usernames"],
        }
        return result
    finally:
        conn.close()


def render_firewall(ssh_port: int, report: dict) -> str:
    if not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
        raise MigrationError("SSH-порт должен быть от 1 до 65535")
    rules = []

    def add(port, proto, comment):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return
        if proto not in {"tcp", "udp"}:
            return
        comment = re.sub(r"[^A-Za-z0-9 _./:+-]", "", comment)[:80]
        key = (port, proto)
        for existing in rules:
            if existing[0] == key:
                if comment and comment not in existing[1]:
                    existing[1].append(comment)
                return
        rules.append((key, [comment] if comment else []))

    add(ssh_port, "tcp", "SSH")
    web = report.get("web_port")
    if isinstance(web, int):
        add(web, "tcp", "panel")
    sub = report.get("sub_port")
    if isinstance(sub, int):
        note = "subscription"
        if report.get("sub_enable") is False:
            note = "subscription subEnable=false"
        add(sub, "tcp", note)
    for item in report.get("inbounds") or []:
        port = item.get("port")
        proto_name = item.get("protocol") or "inbound"
        network = item.get("network") or ""
        label = (proto_name + " " + network).strip()
        for l4 in item.get("l4") or ["tcp"]:
            add(port, l4, label)

    ufw = []
    nft_ports = []
    for (port, proto), comments in rules:
        comment = ", ".join(comments)
        ufw.append("ufw allow %s/%s   # %s" % (port, proto, comment))
        nft_ports.append("        %s dport %s accept   # %s" % (proto, port, comment))
    ufw.append("# ufw enable   # скрипт это не выполняет: policy deny может оборвать текущую SSH-сессию")

    nft = [
        "#!/usr/sbin/nft -f",
        "",
        "# Образец по документации 3x-ui. Скрипт этот файл не записывает и не выполняет.",
        "# flush ruleset вместе с policy drop оборвёт SSH, если порта этой сессии нет в списке.",
        "flush ruleset",
        "",
        "table inet filter {",
        "    chain input {",
        "        type filter hook input priority 0; policy drop;",
        "",
        '        iif "lo" accept',
        "        ct state established,related accept",
        "        icmp type echo-request accept",
    ]
    nft.extend(nft_ports)
    nft.extend(
        [
            "    }",
            "",
            "    chain forward {",
            "        type filter hook forward priority 0; policy drop;",
            "    }",
            "",
            "    chain output {",
            "        type filter hook output priority 0; policy accept;",
            "    }",
            "}",
            "",
        ]
    )
    return "\n".join(ufw) + "\n\n" + "\n".join(nft)


def show(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return "".join(char for char in text if char == "\t" or ord(char) >= 32)


def format_summary(report: dict) -> str:
    lines = []
    lines.append("Инбаундов: %s" % report["inbound_count"])
    lines.append("Из них reality: %s" % report["reality_count"])
    lines.append("Клиентов в settings.clients: %s" % report["json_clients"])
    lines.append("Записей в settings.peers: %s" % report["json_peers"])
    for name, count in report["client_rows"].items():
        if count is None:
            shown = "таблицы нет"
        else:
            shown = str(count)
        lines.append("Таблица %s: %s" % (name, shown))
    lines.append("Пользователи панели (без пароля): %s" % (", ".join(show(name) for name in report["usernames"]) or "нет"))
    lines.append("webPort: %s" % (report["web_port"] if report["web_port"] is not None else "не задан"))
    lines.append("subPort: %s" % (report["sub_port"] if report["sub_port"] is not None else "не задан"))
    if report.get("two_factor_enabled"):
        lines.append("Двухфакторный вход включён. Секрет не показывается и не отключается.")
    bots = report["bots"]
    if bots["telegram_enabled"] or bots["telegram_token_set"]:
        lines.append(
            "Telegram-бот: enable=%s, токен в базе %s. Скрипт его не выключает и не показывает. "
            "После запуска оба сервера могут слать одни и те же уведомления."
            % (bots["telegram_enabled"], "задан" if bots["telegram_token_set"] else "пуст")
        )
    if bots["discord_enabled"] or bots["discord_token_set"]:
        lines.append(
            "Discord-бот: enable=%s, токен в базе %s. Скрипт его не выключает и не показывает. "
            "После запуска оба сервера могут слать одни и те же уведомления."
            % (bots["discord_enabled"], "задан" if bots["discord_token_set"] else "пуст")
        )
    if report["api_token_rows"]:
        lines.append(
            "api_tokens: %s строк. В базе только SHA-256. Старый plaintext-токен, "
            "если он сохранён у оператора, останется рабочим. Токен не показывается."
            % report["api_token_rows"]
        )
    nodes = report["nodes"]
    if nodes["nonzero_node_id"] or nodes["origin_node_guid_ids"]:
        lines.append(
            "Инбаунды с ненулевым node_id: %s. С origin_node_guid: %s. "
            "Локальный Xray на новом сервере может их не слушать. Поля не меняются."
            % (nodes["nonzero_node_id"] or "нет", nodes["origin_node_guid_ids"] or "нет")
        )
    elif nodes["table_rows"]:
        lines.append(
            "В nodes %s строк, но node_id у инбаундов пустой. Таблицу nodes скрипт не меняет."
            % nodes["table_rows"]
        )
    for node in nodes["listed"]:
        lines.append(
            "Нода: name=%s address=%s port=%s"
            % (
                show(node["name"]),
                show(node["address"]),
                node["port"] if node["port"] is not None else "-",
            )
        )
    if report["addresses"]:
        lines.append("Адреса ссылок (share_addr и hosts.address), без правок до отдельного подтверждения:")
        seen = []
        for item in report["addresses"]:
            key = (item["field"], item["value"])
            if key in seen:
                continue
            seen.append(key)
            lines.append("  %s = %s" % (item["field"], show(item["value"])))
    if report["certs"]:
        lines.append("Пути сертификатов:")
        for cert in report["certs"]:
            if cert["kind"] != "absolute":
                lines.append(
                    "  %s: не файл по абсолютному пути (%s), копироваться не будет"
                    % (", ".join(cert["sources"]), cert["kind"])
                )
                continue
            flag = " включённый TLS" if cert["enabled_tls"] else ""
            lines.append("  %s%s (%s)" % (show(cert["path"]), flag, ", ".join(cert["sources"])))
    if report["inline_tls_ids"]:
        lines.append(
            "У инбаундов %s сертификат TLS встроен в JSON. Файл для них не обязателен, содержимое не печатается."
            % report["inline_tls_ids"]
        )
    if report["reality_bad"]:
        for item in report["reality_bad"]:
            lines.append(
                "ВНИМАНИЕ: инбаунд id=%s, security=reality, нет полей: %s"
                % (item["id"], ", ".join(item["missing"]))
            )
    if report["unknown_traffic_tables"]:
        lines.append(
            "Есть таблицы трафика вне списка очистки, они не трогаются: %s"
            % ", ".join(report["unknown_traffic_tables"])
        )
    for item in report["inbounds"]:
        lines.append(
            "  id=%s %s port=%s network=%s security=%s enable=%s l4=%s"
            % (
                item["id"],
                show(item["protocol"]),
                item["port"],
                show(item["network"]),
                show(item["security"]),
                item["enable"],
                ",".join(item["l4"]),
            )
        )
    return "\n".join(lines) + "\n"


def _dump_authorizer(action, _arg1, _arg2, _db_name, _source):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def load_dump(dump_path: str, db_path: str) -> None:
    dest = Path(db_path)
    if dest.exists():
        raise MigrationError("файл назначения уже есть: копия не перезаписывается молча")
    script = Path(dump_path).read_text(encoding="utf-8", errors="strict")
    if "\x00" in script[:200]:
        raise MigrationError("дамп не похож на SQL-текст")
    conn = sqlite3.connect(db_path)
    try:
        conn.set_authorizer(_dump_authorizer)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.executescript(script)
    except sqlite3.Error as exc:
        conn.close()
        dest.unlink(missing_ok=True)
        raise MigrationError("не удалось загрузить SQL-дамп") from exc
    conn.close()


def inbound_count(path: str) -> int:
    file = Path(path)
    if not file.exists():
        return 0
    try:
        conn = connect(path, readonly=True)
    except sqlite3.Error as exc:
        raise MigrationError("файл базы не открывается как SQLite") from exc
    try:
        if not table_exists(conn, "inbounds"):
            raise MigrationError("в существующей базе нет таблицы inbounds")
        row = conn.execute("SELECT COUNT(*) FROM " + qident("inbounds")).fetchone()
        return int(row[0])
    finally:
        conn.close()


def read_report(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(data) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def cmd_inspect(args) -> int:
    conn = connect(args.db, readonly=True)
    try:
        report = build_report(conn)
    finally:
        conn.close()
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os_chmod_private(args.out)
    else:
        dump_json(report)
    return 0


def os_chmod_private(path: str) -> None:
    try:
        Path(path).chmod(0o600)
    except OSError:
        pass


def cmd_summary(args) -> int:
    sys.stdout.write(format_summary(read_report(args.report)))
    return 0


def cmd_firewall(args) -> int:
    sys.stdout.write(render_firewall(args.ssh_port, read_report(args.report)))
    return 0


def cmd_strip(args) -> int:
    pairs = [(old, new) for old, new in (args.replace or [])]
    result = strip_database(args.db, pairs)
    dump_json(result)
    return 0


def cmd_verify(args) -> int:
    baseline_report = read_report(args.baseline)
    baseline = {
        "inbounds": baseline_report["baseline"]["inbounds"],
        "hosts": baseline_report["baseline"]["hosts"],
        "web_port": baseline_report.get("web_port"),
        "reality_count": baseline_report.get("reality_count"),
    }
    result = verify_database(args.db, baseline, args.allow or [], args.phase)
    dump_json(result)
    return 0 if result["ok"] else 1


def cmd_count(args) -> int:
    sys.stdout.write(str(inbound_count(args.db)) + "\n")
    return 0


def cmd_load_dump(args) -> int:
    load_dump(args.dump, args.db)
    return 0


def cmd_cert_paths(args) -> int:
    report = read_report(args.report)
    for cert in report.get("certs") or []:
        path = cert.get("path") or ""
        if cert.get("kind") != "absolute" or not path:
            continue
        if "\t" in path or "\n" in path or "\r" in path:
            sys.stderr.write("пропущен путь сертификата с управляющим символом\n")
            continue
        flag = "1" if cert.get("enabled_tls") else "0"
        sys.stdout.write("%s\t%s\n" % (path, flag))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Очистка копии базы 3x-ui от клиентов")
    sub = parser.add_subparsers(dest="cmd", required=True)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("db")
    inspect.add_argument("--out")
    inspect.set_defaults(func=cmd_inspect)

    summary = sub.add_parser("summary")
    summary.add_argument("report")
    summary.set_defaults(func=cmd_summary)

    firewall = sub.add_parser("firewall")
    firewall.add_argument("report")
    firewall.add_argument("--ssh-port", type=int, required=True)
    firewall.set_defaults(func=cmd_firewall)

    strip = sub.add_parser("strip")
    strip.add_argument("db")
    strip.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"))
    strip.set_defaults(func=cmd_strip)

    verify = sub.add_parser("verify")
    verify.add_argument("db")
    verify.add_argument("--baseline", required=True)
    verify.add_argument("--allow", action="append")
    verify.add_argument("--phase", choices=("copy", "live"), default="copy")
    verify.set_defaults(func=cmd_verify)

    count = sub.add_parser("inbound-count")
    count.add_argument("db")
    count.set_defaults(func=cmd_count)

    load = sub.add_parser("load-dump")
    load.add_argument("dump")
    load.add_argument("db")
    load.set_defaults(func=cmd_load_dump)

    certs = sub.add_parser("cert-paths")
    certs.add_argument("report")
    certs.set_defaults(func=cmd_cert_paths)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MigrationError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except sqlite3.Error:
        sys.stderr.write("ошибка SQLite, подробности базы в лог не пишутся\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
