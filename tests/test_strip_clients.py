#!/usr/bin/env python3
"""Проверки очистки копии базы: клиенты уходят, параметры инбаундов остаются."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import strip_clients as sc

PRIVATE_KEY = "PRIVATE_KEY_SENTINEL"
BOT_TOKEN = "BOT_TOKEN_SENTINEL"
PANEL_HASH = "PANEL_PASSWORD_HASH"
SS_PASSWORD = "SS_SERVER_PASSWORD"
WG_SECRET = "WG_SERVER_SECRET"
API_HASH = "API_TOKEN_HASH"
CLIENT_UUID = "11111111-2222-3333-4444-555555555555"


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


REALITY_STREAM = {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
        "dest": "www.microsoft.com:443",
        "serverNames": ["www.microsoft.com"],
        "privateKey": PRIVATE_KEY,
        "shortIds": ["abcd12", ""],
        "fingerprint": "chrome",
        "spiderX": "/",
    },
}
GRPC_STREAM = {
    "network": "grpc",
    "security": "tls",
    "grpcSettings": {"serviceName": "keep-service", "multiMode": False},
    "tlsSettings": {
        "certificates": [
            {
                "certificateFile": "/root/cert/fullchain.pem",
                "keyFile": "/root/cert/privkey.pem",
            }
        ]
    },
}
WS_STREAM = {
    "network": "ws",
    "security": "none",
    "wsSettings": {"path": "/ws-keep", "host": "cdn.example.com"},
}
XHTTP_STREAM = {
    "network": "xhttp",
    "security": "none",
    "xhttpSettings": {"path": "/xhttp-keep", "mode": "auto"},
}
TCP_STREAM = {"network": "tcp", "security": "none", "tcpSettings": {"header": {"type": "none"}}}


SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT);
CREATE TABLE inbounds (
  id INTEGER PRIMARY KEY,
  user_id INTEGER,
  up INTEGER,
  down INTEGER,
  total INTEGER,
  remark TEXT,
  enable INTEGER,
  expiry_time INTEGER,
  traffic_reset TEXT,
  listen TEXT,
  port INTEGER,
  protocol TEXT,
  settings TEXT,
  stream_settings TEXT,
  tag TEXT,
  sniffing TEXT,
  node_id INTEGER,
  share_addr TEXT,
  share_addr_strategy TEXT,
  disable_flow INTEGER,
  sub_sort_index INTEGER,
  exclude_from_sub INTEGER,
  origin_node_guid TEXT
);
CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
CREATE TABLE clients (id INTEGER PRIMARY KEY, email TEXT, uuid TEXT, password TEXT);
CREATE TABLE client_inbounds (client_id INTEGER, inbound_id INTEGER);
CREATE TABLE client_traffics (id INTEGER PRIMARY KEY, email TEXT, up INTEGER, down INTEGER);
CREATE TABLE inbound_client_ips (id INTEGER PRIMARY KEY, client_email TEXT, ips TEXT);
CREATE TABLE client_hwids (id INTEGER PRIMARY KEY, sub_id TEXT);
CREATE TABLE client_external_links (id INTEGER PRIMARY KEY, client_id INTEGER, value TEXT);
CREATE TABLE client_groups (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE history_of_seeders (id INTEGER PRIMARY KEY, seeder_name TEXT);
CREATE TABLE hosts (
  id INTEGER PRIMARY KEY,
  inbound_id INTEGER,
  address TEXT,
  remark TEXT,
  updated_at INTEGER
);
CREATE TABLE nodes (
  id INTEGER PRIMARY KEY,
  name TEXT,
  address TEXT,
  port INTEGER,
  api_token TEXT
);
CREATE TABLE api_tokens (id INTEGER PRIMARY KEY, name TEXT, token TEXT);
CREATE TABLE outbound_traffics (id INTEGER PRIMARY KEY, tag TEXT, up INTEGER, down INTEGER);
CREATE TABLE node_client_traffics (
  id INTEGER PRIMARY KEY, node_id INTEGER, email TEXT, up INTEGER, down INTEGER
);
CREATE TABLE client_global_traffics (
  id INTEGER PRIMARY KEY, master_guid TEXT, email TEXT, up INTEGER, down INTEGER
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def seed(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO users (username, password) VALUES (?, ?)",
        ("admin", PANEL_HASH),
    )
    rows = [
        (
            1,
            "reality",
            "vless",
            443,
            dumps(
                {
                    "clients": [
                        {
                            "id": CLIENT_UUID,
                            "email": "a@b.c",
                            "flow": "xtls-rprx-vision",
                        }
                    ],
                    "decryption": "none",
                    "fallbacks": [{"dest": 80}],
                }
            ),
            dumps(REALITY_STREAM),
            1,
            None,
            "",
            "203.0.113.10",
        ),
        (
            2,
            "grpc",
            "vless",
            2053,
            dumps(
                {
                    "clients": [{"id": CLIENT_UUID, "email": "g@b.c"}],
                    "decryption": "none",
                }
            ),
            dumps(GRPC_STREAM),
            1,
            None,
            "",
            "",
        ),
        (
            3,
            "ws",
            "vless",
            8443,
            dumps({"clients": {"id": "not-a-list"}, "decryption": "none"}),
            dumps(WS_STREAM),
            1,
            None,
            "",
            "",
        ),
        (
            4,
            "xhttp",
            "vless",
            8444,
            dumps({"decryption": "none"}),
            dumps(XHTTP_STREAM),
            1,
            None,
            "",
            "",
        ),
        (
            5,
            "tcp",
            "vless",
            8445,
            dumps({"clients": None, "decryption": "none"}),
            dumps(TCP_STREAM),
            0,
            None,
            "",
            "",
        ),
        (
            6,
            "ss",
            "shadowsocks",
            8388,
            dumps(
                {
                    "method": "2022-blake3-aes-128-gcm",
                    "password": SS_PASSWORD,
                    "network": "tcp,udp",
                    "clients": [{"password": "CLIENT_PW", "email": "s@b.c"}],
                }
            ),
            dumps({"network": "tcp", "security": "none"}),
            1,
            None,
            "",
            "",
        ),
        (
            7,
            "wg",
            "wireguard",
            51820,
            dumps(
                {
                    "mtu": 1420,
                    "secretKey": WG_SECRET,
                    "peers": [{"publicKey": "PEER", "allowedIPs": ["10.0.0.2/32"]}],
                    "clients": [{"email": "wg@b.c", "privateKey": "CLIENT_WG"}],
                }
            ),
            dumps({}),
            1,
            None,
            "",
            "",
        ),
        (
            8,
            "hy",
            "hysteria",
            36712,
            dumps({"version": 2, "clients": [{"auth": "HY", "email": "h@b.c"}]}),
            dumps({}),
            1,
            None,
            "",
            "",
        ),
        (
            9,
            "mt",
            "mtproto",
            443,
            dumps(
                {
                    "fakeTlsDomain": "www.cloudflare.com",
                    "secret": "eeSERVER",
                    "clients": [{"email": "m@b.c", "secret": "eeCLIENT"}],
                }
            ),
            "",
            1,
            None,
            "",
            "",
        ),
        (
            10,
            "node",
            "vless",
            9443,
            dumps({"clients": [{"id": CLIENT_UUID, "email": "n@b.c"}], "decryption": "none"}),
            dumps(REALITY_STREAM),
            1,
            7,
            "origin-guid",
            "203.0.113.10",
        ),
    ]
    for row in rows:
        conn.execute(
            """
            INSERT INTO inbounds (
              id, remark, protocol, port, settings, stream_settings, enable,
              node_id, origin_node_guid, share_addr, up, down, total, expiry_time,
              traffic_reset, listen, tag, sniffing, share_addr_strategy,
              disable_flow, sub_sort_index, exclude_from_sub, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 10, 20, 999, 123, 'never', '', ?, '{}', 'custom', 0, 1, 0, 1)
            """,
            (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], "in-%s" % row[0]),
        )
    settings = {
        "webPort": "2053",
        "subPort": "2096",
        "subEnable": "true",
        "webCertFile": "/root/cert/panel.crt",
        "webKeyFile": "/root/cert/panel.key",
        "subCertFile": "/root/cert/sub.crt",
        "subKeyFile": "/root/cert/sub.key",
        "tgBotEnable": "true",
        "tgBotToken": BOT_TOKEN,
        "discordBotEnable": "false",
        "discordBotToken": "",
        "twoFactorEnable": "false",
    }
    for key, value in settings.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.execute("INSERT INTO clients (email, uuid, password) VALUES ('a@b.c', ?, 'pw')", (CLIENT_UUID,))
    conn.execute("INSERT INTO client_inbounds (client_id, inbound_id) VALUES (1, 1)")
    conn.execute("INSERT INTO client_traffics (email, up, down) VALUES ('a@b.c', 5, 6)")
    conn.execute("INSERT INTO inbound_client_ips (client_email, ips) VALUES ('a@b.c', '[]')")
    conn.execute("INSERT INTO client_hwids (sub_id) VALUES ('sub')")
    conn.execute("INSERT INTO client_external_links (client_id, value) VALUES (1, 'vless://old')")
    conn.execute("INSERT INTO client_groups (name) VALUES ('vip')")
    conn.execute("INSERT INTO history_of_seeders (seeder_name) VALUES ('ClientsTable')")
    conn.execute(
        "INSERT INTO hosts (inbound_id, address, remark, updated_at) VALUES (1, '203.0.113.10', 'direct', 1)"
    )
    conn.execute(
        "INSERT INTO hosts (inbound_id, address, remark, updated_at) VALUES (1, 'cdn.example.com', 'cdn', 1)"
    )
    conn.execute(
        "INSERT INTO nodes (name, address, port, api_token) VALUES ('de', 'node.example.com', 2053, 'NODE_TOKEN_SENTINEL')"
    )
    conn.execute("INSERT INTO api_tokens (name, token) VALUES ('default', ?)", (API_HASH,))
    conn.execute("INSERT INTO outbound_traffics (tag, up, down) VALUES ('direct', 3, 4)")
    conn.execute(
        "INSERT INTO node_client_traffics (node_id, email, up, down) VALUES (1, 'a@b.c', 9, 9)"
    )
    conn.execute(
        "INSERT INTO client_global_traffics (master_guid, email, up, down) VALUES ('guid', 'a@b.c', 8, 8)"
    )
    conn.commit()
    conn.close()


class StripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "x-ui.db")
        seed(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def streams(self):
        conn = connect(self.db)
        rows = conn.execute("SELECT id, stream_settings FROM inbounds ORDER BY id").fetchall()
        conn.close()
        return {row["id"]: row["stream_settings"] for row in rows}

    def settings_of(self, inbound_id):
        conn = connect(self.db)
        row = conn.execute("SELECT settings FROM inbounds WHERE id = ?", (inbound_id,)).fetchone()
        conn.close()
        return json.loads(row["settings"])

    def test_strip_removes_clients_and_keeps_inbound_parameters(self):
        before_streams = self.streams()
        sc.strip_database(self.db, [])
        after_streams = self.streams()
        self.assertEqual(before_streams, after_streams)

        reality = self.settings_of(1)
        self.assertEqual(reality["clients"], [])
        self.assertEqual(reality["decryption"], "none")
        self.assertEqual(reality["fallbacks"], [{"dest": 80}])
        self.assertNotIn(CLIENT_UUID, json.dumps(reality))

        grpc = json.loads(after_streams[2])
        self.assertEqual(grpc["grpcSettings"]["serviceName"], "keep-service")
        self.assertEqual(json.loads(after_streams[3])["wsSettings"]["path"], "/ws-keep")
        self.assertEqual(json.loads(after_streams[4])["xhttpSettings"]["path"], "/xhttp-keep")
        self.assertEqual(json.loads(after_streams[5])["tcpSettings"]["header"]["type"], "none")

        self.assertEqual(self.settings_of(3)["clients"], [])
        self.assertEqual(self.settings_of(4)["clients"], [])
        self.assertEqual(self.settings_of(5)["clients"], [])

        ss = self.settings_of(6)
        self.assertEqual(ss["clients"], [])
        self.assertEqual(ss["method"], "2022-blake3-aes-128-gcm")
        self.assertEqual(ss["password"], SS_PASSWORD)
        self.assertEqual(ss["network"], "tcp,udp")

        wg = self.settings_of(7)
        self.assertEqual(wg["clients"], [])
        self.assertEqual(wg["peers"], [])
        self.assertEqual(wg["secretKey"], WG_SECRET)
        self.assertEqual(wg["mtu"], 1420)

        self.assertEqual(self.settings_of(8)["version"], 2)
        self.assertEqual(self.settings_of(8)["clients"], [])
        mt = self.settings_of(9)
        self.assertEqual(mt["fakeTlsDomain"], "www.cloudflare.com")
        self.assertEqual(mt["secret"], "eeSERVER")
        self.assertEqual(mt["clients"], [])

        conn = connect(self.db)
        for name in sc.CLIENT_TABLES:
            count = conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            self.assertEqual(count, 0, name)
        self.assertEqual(
            conn.execute("SELECT seeder_name FROM history_of_seeders").fetchone()[0],
            "ClientsTable",
        )
        self.assertEqual(conn.execute("SELECT password FROM users").fetchone()[0], PANEL_HASH)
        self.assertEqual(conn.execute("SELECT token FROM api_tokens").fetchone()[0], API_HASH)
        self.assertEqual(conn.execute("SELECT up FROM outbound_traffics").fetchone()[0], 3)
        traffic = conn.execute("SELECT up, down, total, enable, expiry_time, node_id FROM inbounds WHERE id = 1").fetchone()
        self.assertEqual(tuple(traffic), (0, 0, 999, 1, 123, None))
        node = conn.execute(
            "SELECT node_id, origin_node_guid, share_addr FROM inbounds WHERE id = 10"
        ).fetchone()
        self.assertEqual(node["node_id"], 7)
        self.assertEqual(node["origin_node_guid"], "origin-guid")
        self.assertEqual(node["share_addr"], "203.0.113.10")
        conn.close()

        first = self.settings_of(1)
        sc.strip_database(self.db, [])
        self.assertEqual(first, self.settings_of(1))
        self.assertEqual(before_streams, self.streams())

    def test_address_replace_does_not_touch_reality(self):
        before = self.streams()
        sc.strip_database(self.db, [("203.0.113.10", "198.51.100.8")])
        self.assertEqual(before, self.streams())
        conn = connect(self.db)
        shares = [row[0] for row in conn.execute("SELECT share_addr FROM inbounds WHERE id IN (1, 10)")]
        self.assertEqual(shares, ["198.51.100.8", "198.51.100.8"])
        addresses = [row[0] for row in conn.execute("SELECT address FROM hosts ORDER BY id")]
        self.assertEqual(addresses, ["198.51.100.8", "cdn.example.com"])
        remark = conn.execute("SELECT remark FROM hosts WHERE id = 2").fetchone()[0]
        self.assertEqual(remark, "cdn")
        conn.close()
        reality = json.loads(before[1])
        self.assertEqual(reality["realitySettings"]["serverNames"], ["www.microsoft.com"])
        self.assertEqual(reality["realitySettings"]["privateKey"], PRIVATE_KEY)

    def test_bad_settings_leave_the_database_unchanged(self):
        conn = sqlite3.connect(self.db)
        original = Path(self.db).read_bytes()
        conn.execute("UPDATE inbounds SET settings = ? WHERE id = 1", ("{",))
        conn.commit()
        conn.close()
        before = Path(self.db).read_bytes()
        with self.assertRaises(sc.MigrationError) as caught:
            sc.strip_database(self.db, [])
        self.assertIn("id=1", str(caught.exception))
        self.assertEqual(Path(self.db).read_bytes(), before)
        self.assertNotEqual(before, original)

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE inbounds SET settings = ? WHERE id = 1",
            (dumps({"clients": [], "peers": {"publicKey": "x"}, "decryption": "none"}),),
        )
        conn.commit()
        conn.close()
        before = Path(self.db).read_bytes()
        with self.assertRaises(sc.MigrationError) as caught:
            sc.strip_database(self.db, [])
        self.assertIn("peers", str(caught.exception))
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_missing_client_table_is_skipped(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE client_hwids")
        conn.commit()
        conn.close()
        result = sc.strip_database(self.db, [])
        self.assertIsNone(result["deleted"]["client_hwids"])
        self.assertEqual(result["deleted"]["clients"], 1)

    def test_report_hides_secrets_and_counts_reality(self):
        conn = sc.connect(self.db, readonly=True)
        try:
            report = sc.build_report(conn)
        finally:
            conn.close()
        blob = json.dumps(report)
        for secret in (PRIVATE_KEY, BOT_TOKEN, PANEL_HASH, SS_PASSWORD, WG_SECRET, API_HASH, CLIENT_UUID, "NODE_TOKEN_SENTINEL"):
            self.assertNotIn(secret, blob)
        summary = sc.format_summary(report)
        for secret in (PRIVATE_KEY, BOT_TOKEN, PANEL_HASH, SS_PASSWORD, WG_SECRET, API_HASH, "NODE_TOKEN_SENTINEL"):
            self.assertNotIn(secret, summary)
        self.assertEqual(report["inbound_count"], 10)
        self.assertEqual(report["reality_count"], 2)
        self.assertEqual(report["json_clients"], 7)
        self.assertEqual(report["nodes"]["nonzero_node_id"], [10])
        self.assertEqual(report["nodes"]["origin_node_guid_ids"], [10])
        self.assertEqual(report["nodes"]["listed"][0]["address"], "node.example.com")
        self.assertTrue(report["bots"]["telegram_token_set"])
        self.assertEqual(report["web_port"], 2053)
        paths = {item["path"] for item in report["certs"] if item["kind"] == "absolute"}
        self.assertIn("/root/cert/fullchain.pem", paths)
        self.assertIn("/root/cert/panel.crt", paths)
        enabled = [item for item in report["certs"] if item["path"] == "/root/cert/fullchain.pem"][0]
        self.assertTrue(enabled["enabled_tls"])
        text = sc.render_firewall(2222, report)
        self.assertIn("ufw allow 2222/tcp   # SSH", text)
        self.assertIn("ufw allow 2053/tcp", text)
        self.assertIn("ufw allow 51820/udp", text)
        self.assertIn("ufw allow 8388/tcp", text)
        self.assertIn("ufw allow 8388/udp", text)
        self.assertIn("# ufw enable", text)
        self.assertNotIn("\nufw enable\n", "\n" + text)
        self.assertIn("tcp dport 2222 accept", text)
        self.assertIn("policy drop", text)
        self.assertIn("Telegram-бот", summary)

    def test_verify_and_live_phase(self):
        conn = sc.connect(self.db, readonly=True)
        try:
            baseline = sc.build_report(conn)
        finally:
            conn.close()
        sc.strip_database(self.db, [])
        checked = sc.verify_database(
            self.db,
            {
                "inbounds": baseline["baseline"]["inbounds"],
                "hosts": baseline["baseline"]["hosts"],
                "web_port": baseline["web_port"],
                "reality_count": baseline["reality_count"],
            },
            [],
            "copy",
        )
        self.assertTrue(checked["ok"], checked["errors"])
        self.assertEqual(checked["reality_fields_ok"], 2)
        self.assertEqual(checked["client_rows"]["clients"], 0)
        self.assertEqual(checked["client_rows"]["client_traffics"], 0)

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE hosts SET updated_at = 99 WHERE id = 1")
        conn.commit()
        conn.close()
        live = sc.verify_database(
            self.db,
            {
                "inbounds": baseline["baseline"]["inbounds"],
                "hosts": baseline["baseline"]["hosts"],
                "web_port": baseline["web_port"],
                "reality_count": baseline["reality_count"],
            },
            [],
            "live",
        )
        self.assertTrue(live["ok"], live["errors"])
        copy_again = sc.verify_database(
            self.db,
            {
                "inbounds": baseline["baseline"]["inbounds"],
                "hosts": baseline["baseline"]["hosts"],
                "web_port": baseline["web_port"],
                "reality_count": baseline["reality_count"],
            },
            [],
            "copy",
        )
        self.assertFalse(copy_again["ok"])

    def test_reality_missing_dest_fails_verify(self):
        broken = dict(REALITY_STREAM)
        broken["realitySettings"] = dict(REALITY_STREAM["realitySettings"])
        del broken["realitySettings"]["dest"]
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE inbounds SET stream_settings = ? WHERE id = 1", (dumps(broken),))
        conn.execute("UPDATE inbounds SET settings = ? WHERE id = 1", (dumps({"clients": [], "decryption": "none"}),))
        conn.commit()
        conn.close()
        conn = sc.connect(self.db, readonly=True)
        try:
            report = sc.build_report(conn)
        finally:
            conn.close()
        checked = sc.verify_database(
            self.db,
            {"inbounds": report["baseline"]["inbounds"], "hosts": report["baseline"]["hosts"], "web_port": 2053, "reality_count": 2},
            [],
            "copy",
        )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("dest" in item and "id=1" in item for item in checked["errors"]))
        self.assertNotIn(PRIVATE_KEY, "\n".join(checked["errors"]))

    def test_load_dump_and_inbound_count(self):
        missing = str(Path(self.tmp.name) / "absent.db")
        self.assertEqual(sc.inbound_count(missing), 0)
        self.assertEqual(sc.inbound_count(self.db), 10)
        dump = str(Path(self.tmp.name) / "out.sql")
        dest = str(Path(self.tmp.name) / "restored.db")
        Path(dump).write_text(
            "CREATE TABLE inbounds (id INTEGER PRIMARY KEY, settings TEXT, stream_settings TEXT);\n"
            "INSERT INTO inbounds (id, settings, stream_settings) VALUES (1, '{\"clients\":[]}', '{}');\n",
            encoding="utf-8",
        )
        sc.load_dump(dump, dest)
        self.assertEqual(sc.inbound_count(dest), 1)
        attached = str(Path(self.tmp.name) / "attached.db")
        payload = str(Path(self.tmp.name) / "should-not-exist.db")
        bad = str(Path(self.tmp.name) / "bad.sql")
        Path(bad).write_text(
            "ATTACH DATABASE '%s' AS evil;\n" % payload,
            encoding="utf-8",
        )
        with self.assertRaises(sc.MigrationError):
            sc.load_dump(bad, attached)
        self.assertFalse(Path(attached).exists())
        self.assertFalse(Path(payload).exists())

    def test_address_rejects_metacharacters(self):
        with self.assertRaises(sc.MigrationError):
            sc.validate_address("1.2.3.4;rm", "новый адрес")
        self.assertEqual(sc.validate_address("почта.example", "адрес"), "почта.example")

    def test_l4(self):
        self.assertEqual(sc.l4_for("vless", {"network": "grpc"}, {}), ["tcp"])
        self.assertEqual(sc.l4_for("vless", {"network": "xhttp"}, {}), ["tcp"])
        self.assertEqual(sc.l4_for("vless", {"network": "ws"}, {}), ["tcp"])
        self.assertEqual(sc.l4_for("wireguard", {}, {}), ["udp"])
        self.assertEqual(sc.l4_for("shadowsocks", {}, {"network": "tcp,udp"}), ["tcp", "udp"])
        self.assertEqual(sc.l4_for("vless", {"network": "kcp"}, {}), ["udp"])


if __name__ == "__main__":
    unittest.main()
