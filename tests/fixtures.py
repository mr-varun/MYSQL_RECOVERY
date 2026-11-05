"""Build healthy test data folders with each server build: a `shop` database with InnoDB and MyISAM
tables, a view, a procedure and a trigger, shut down cleanly.

    python tests/fixtures.py            -> tests/fixtures/<server label>/data  (+ manifest.json)
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
import mysql_rescue  # noqa: E402
import servers as servers_mod  # noqa: E402
from server import TempServer  # noqa: E402

FIXTURES = HERE / "fixtures"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CUSTOMERS = 1000
ORDERS = 3000
SHOP_SQL = """
CREATE DATABASE shop;
USE shop;
CREATE TABLE customers (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(60) NOT NULL, city VARCHAR(30),
  balance DECIMAL(12,2), note TEXT, joined DATE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE orders (id INT PRIMARY KEY AUTO_INCREMENT, customer_id INT NOT NULL, total DECIMAL(10,2),
  status VARCHAR(12), KEY (customer_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE audit_log (id INT PRIMARY KEY AUTO_INCREMENT, msg VARCHAR(200)) ENGINE=MyISAM DEFAULT CHARSET=utf8mb4;
CREATE VIEW hubli_customers AS SELECT id, name FROM customers WHERE city = 'Hubli';
CREATE TRIGGER orders_audit AFTER INSERT ON orders FOR EACH ROW INSERT INTO audit_log (msg) VALUES (CONCAT('order ', NEW.id));
CREATE PROCEDURE big_spenders() SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id HAVING SUM(total) > 5000;
"""


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, timeout=600, creationflags=CREATE_NO_WINDOW, **kw)


def initialize(server, data):
    """An empty, working data folder for this build."""
    data.mkdir(parents=True)
    if server.family == "mariadb":
        r = run([str(server.tool("mariadb-install-db.exe")), "--datadir=%s" % data])
    elif server.major_minor >= (8, 0):
        r = run([str(server.exe), "--no-defaults", "--initialize-insecure", "--basedir=%s" % server.home,
                 "--datadir=%s" % data, "--console"])
    else:  # MySQL 5.6 has no --initialize: what mysql_install_db.pl does, through --bootstrap
        share = server.home / "share"
        sql = b"CREATE DATABASE IF NOT EXISTS mysql;\nUSE mysql;\n"
        for name in ("mysql_system_tables.sql", "mysql_system_tables_data.sql"):
            sql += (share / name).read_bytes() + b"\n"
        r = run([str(server.exe), "--no-defaults", "--bootstrap", "--basedir=%s" % server.home, "--datadir=%s" % data,
                 "--innodb-log-file-size=8M", "--console"], input=sql)
    if r.returncode != 0:
        raise RuntimeError("initializing %s failed: %s" % (server.label, (r.stderr or r.stdout)[-800:]))


def populate(server, data, workdir):
    ts = TempServer(server, data, workdir, log=lambda m: None)
    ts.start()
    try:
        r = run([str(server.client), "--no-defaults", *ts.conn_args()], input=SHOP_SQL.encode())
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[-500:])
        cities = ["Hubli", "Dharwad", "Belagavi", "Gadag"]
        rows = ",".join("('Customer %d','%s',%d.50,'%s','2026-0%d-1%d')" % (i, cities[i % 4], i * 7, "note " * (i % 30),
                                                                             1 + i % 9, i % 10) for i in range(1, CUSTOMERS + 1))
        orders = ",".join("(%d,%d.25,'%s')" % (1 + i % CUSTOMERS, (i * 13) % 900, ["paid", "open", "cancelled"][i % 3])
                          for i in range(1, ORDERS + 1))
        sql = ("INSERT INTO shop.customers (name, city, balance, note, joined) VALUES %s;\n"
               "INSERT INTO shop.orders (customer_id, total, status) VALUES %s;\n" % (rows, orders))
        r = run([str(server.client), "--no-defaults", *ts.conn_args()], input=sql.encode())  # too long for a command line
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[-500:])
    finally:
        ts.stop()


def build_all(extra_dirs=()):
    found = servers_mod.find_servers(mysql_rescue.server_dirs(extra_dirs))
    FIXTURES.mkdir(exist_ok=True)
    manifest = {}
    for server in found:
        name = server.label.replace(" ", "-").lower()
        target = FIXTURES / name
        if target.exists():
            shutil.rmtree(target)
        started = time.monotonic()
        data = target / "data"
        initialize(server, data)
        populate(server, data, target)
        for leftover in target.glob("*.ini"):
            leftover.unlink()
        manifest[name] = {"server": server.label, "home": str(server.home), "data": str(data),
                          "customers": CUSTOMERS, "orders": ORDERS, "audit_log": ORDERS}
        print("%-20s %5.1f s  %s" % (server.label, time.monotonic() - started, data))
    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    build_all(sys.argv[1:])
