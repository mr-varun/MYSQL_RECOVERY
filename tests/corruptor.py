"""Damage COPIES of the healthy fixtures in the ways data folders break in real life.

Each function takes a server build and a fixture data folder, and returns a new damaged data
folder plus what a perfect rescue would get back. The fixture itself is never changed.
"""
import os
import random
import shutil
import subprocess
from pathlib import Path

from server import TempServer

PAGE = 16384
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EXTRA = 200  # rows committed just before the crash


def _copy(src, dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def _garble(path, offset, length, seed=7):
    rnd = random.Random(seed)
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(bytes(rnd.randrange(256) for _ in range(length)))


def crash(server, fixture, dst, expected):
    """Committed rows the server never flushed, then a killed server: needs crash recovery."""
    data = _copy(fixture, dst / "data")
    ts = TempServer(server, data, dst, log=lambda m: None)
    ts.start()
    rows = ",".join("('Late %d','Hubli',1.00,'after the backup','2026-11-05')" % i for i in range(EXTRA))
    sql = "INSERT INTO shop.customers (name, city, balance, note, joined) VALUES %s;" % rows
    subprocess.run([str(server.client), "--no-defaults", *ts.conn_args()], input=sql.encode(), capture_output=True,
                   creationflags=CREATE_NO_WINDOW, timeout=120)
    ts.kill()  # no shutdown: the pages are only in the redo log
    want = dict(expected, customers=expected["customers"] + EXTRA)
    return data, want


def torn_redo(server, fixture, dst, expected):
    """A crash, and then the redo log damaged: a normal start refuses, recovery has to skip the log."""
    data, want = crash(server, fixture, dst, expected)
    if (data / "#innodb_redo").is_dir():  # MySQL 8.0.30+
        logs = sorted((data / "#innodb_redo").glob("#ib_redo*"), key=lambda p: p.stat().st_mtime)
        targets = [p for p in logs if not p.name.endswith("_tmp")][-1:]
    else:
        targets = [data / "ib_logfile0"]
    for log in targets:
        size = log.stat().st_size
        _garble(log, 2048, min(256 * 1024, size - 2048))  # keep the file header, break the log blocks
    want["_lossy"] = True  # what was only in the redo log may be gone
    return data, want


def bad_page(server, fixture, dst, expected):
    """One garbage page in the middle of a table's .ibd: reading that table crashes the server."""
    data = _copy(fixture, dst / "data")
    ibd = data / "shop" / "customers.ibd"
    pages = ibd.stat().st_size // PAGE
    _garble(ibd, (pages // 2) * PAGE, PAGE)
    want = dict(expected, _lossy=True)
    return data, want


def myisam_index(server, fixture, dst, expected):
    """A MyISAM table whose index file header is broken ("Incorrect key file" / marked as crashed)."""
    data = _copy(fixture, dst / "data")
    myi = data / "shop" / "audit_log.MYI"
    if not myi.exists():
        myi = next((data / "shop").glob("audit_log*.MYI"))
    _garble(myi, 0, 64)
    want = dict(expected, _lossy=True)
    return data, want


DAMAGES = {"healthy": None, "crash": crash, "torn_redo": torn_redo, "bad_page": bad_page, "myisam_index": myisam_index}


def make(name, server, fixture, dst, expected):
    dst.mkdir(parents=True, exist_ok=True)
    if DAMAGES[name] is None:
        return _copy(fixture, dst / "data"), dict(expected)
    return DAMAGES[name](server, Path(fixture), dst, dict(expected))


def remove(path):
    def writable(func, p, _exc):
        os.chmod(p, 0o666)
        func(p)
    shutil.rmtree(path, onerror=writable)
