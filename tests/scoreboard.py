"""Damage every fixture in every way, rescue it, and print what came back.

    python tests/fixtures.py     (once: builds the healthy fixtures)
    python tests/scoreboard.py   -> table on screen + tests/work/scoreboard.json
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
sys.path.insert(0, str(HERE))
import corruptor  # noqa: E402
import mysql_rescue  # noqa: E402
import servers as servers_mod  # noqa: E402

WORK = HERE / "work"
TABLES = ("customers", "orders", "audit_log")


def main(only=None):
    manifest = json.loads((HERE / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    found = {str(s.home).lower(): s for s in servers_mod.find_servers(mysql_rescue.server_dirs())}
    WORK.mkdir(exist_ok=True)
    board = []
    print("%-16s %-13s %-10s %-7s %-6s %s" % ("server", "damage", "status", "tables", "level", "rows (customers/orders/audit_log) vs expected"))
    for name, fx in manifest.items():
        server = found.get(fx["home"].lower())
        if server is None:
            continue
        expected = {t: fx[t] for t in TABLES}
        for damage in corruptor.DAMAGES:
            if only and damage not in only:
                continue
            case = WORK / name / damage
            if case.exists():
                corruptor.remove(case)
            started = time.monotonic()
            try:
                data, want = corruptor.make(damage, server, fx["data"], case, expected)
            except Exception as exc:  # noqa: BLE001
                print("%-16s %-13s could not make the damage: %s" % (server.label, damage, exc))
                continue
            _job, rpt = mysql_rescue.run_pipeline(data, "recover", out=case / "jobs", echo=False)
            got = {}
            for db in rpt["databases"]:
                if db["database"] == "shop":
                    got = {t["table"]: (t["rows"] if t["status"] == "ok" else None) for t in db["tables"]}
            t = rpt["totals"]
            exact = all(got.get(k) == want[k] for k in TABLES)
            row = {"server": server.label, "damage": damage, "status": rpt["status"], "tables_ok": t["tables_ok"],
                   "tables": t["tables"], "level": t["highest_level"], "got": got, "expected": want, "exact": exact,
                   "attempts": rpt["attempts"], "seconds": round(time.monotonic() - started, 1),
                   "original_unchanged": rpt.get("original_unchanged")}
            board.append(row)
            print("%-16s %-13s %-10s %3d/%-3d %-6s %s vs %s%s" % (
                server.label, damage, rpt["status"], t["tables_ok"], t["tables"], t["highest_level"],
                "/".join(str(got.get(k)) for k in TABLES), "/".join(str(want[k]) for k in TABLES),
                "  exact" if exact else ("  (lossy damage)" if want.get("_lossy") else "  MISSING ROWS")))
    (WORK / "scoreboard.json").write_text(json.dumps(board, indent=2), encoding="utf-8")
    return board


if __name__ == "__main__":
    main(set(sys.argv[1:]) or None)
