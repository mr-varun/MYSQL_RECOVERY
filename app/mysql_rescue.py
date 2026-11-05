"""mysql-rescue: get databases back out of a damaged MySQL / MariaDB data folder.

  python mysql_rescue.py diagnose DATA_FOLDER [--out DIR]
  python mysql_rescue.py recover  DATA_FOLDER [--out DIR] [--db NAME ...] [--servers DIR ...]
  python mysql_rescue.py force    DATA_FOLDER [--out DIR] [--db NAME ...]   (server won't start at all)

The original folder is never written to or opened by a server: it is copied into
<out>\\job-<timestamp>-<name>\\source\\ and every server start works on a fresh copy of that.
A server that matches the data (MySQL 5.6 / 8.x, MariaDB) is started on the copy; tables are dumped
one by one; if the server dies on a table it is started again (on a fresh copy) with the next
innodb_force_recovery level and carries on with the tables that are left.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run from anywhere, also with python -I

import inspect_folder
import report as report_mod
import servers as servers_mod
from job import Cancelled, Job, remove_tree
from server import FORCE_LEVELS, ServerError, TempServer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SETTINGS = ROOT / "settings.json"
MODES = {"recover": [0, 1, 2, 3, 4, 5, 6], "force": [1, 2, 3, 4, 5, 6], "diagnose": []}
STAGES = ["copy", "diagnose", "start", "dump", "combine", "report"]
STAGE_NAMES = {"copy": "Copy the data folder", "diagnose": "Diagnose (no server)", "start": "Start a server on the copy",
               "dump": "Dump table by table", "combine": "One .sql per database", "report": "Report"}


class OutputFolderError(Exception):
    pass


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_out():
    out = ROOT / "jobs"
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / (".write-test-%d" % os.getpid())
        probe.write_bytes(b"ok")
        probe.unlink()
        return out
    except OSError:
        return Path.home() / "Documents" / "mysql-rescue-jobs"


def server_dirs(extra=()):
    return [*load_settings().get("server_dirs", []), *extra]


def _list_tables(ts, database):
    """[(name, type, engine)] as the server sees them."""
    try:
        rows = ts.query("SELECT TABLE_NAME, TABLE_TYPE, IFNULL(ENGINE,'') FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = '%s'" % database.replace("'", "''"), timeout=300)
        return [(r[0], r[1], r[2] if len(r) > 2 else "") for r in rows]
    except ServerError:
        rows = ts.query("SHOW FULL TABLES FROM `%s`" % database.replace("`", "``"), timeout=300)
        return [(r[0], "VIEW" if len(r) > 1 and r[1] == "VIEW" else "BASE TABLE", "") for r in rows]


def run_pipeline(folder, mode="recover", out=None, databases=None, extra_server_dirs=(), job_hook=None, echo=True):
    """Rescue one data folder. Returns (job, report dict).
    mode:      recover (normal start first, then recovery levels 1-6) | force (levels 1-6) | diagnose
    databases: names to rescue (default: every user database in the folder)
    job_hook(job) is called right after the job folder exists (windows attach listeners there)."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    data = inspect_folder.find_data_dir(folder)
    out = Path(out) if out else default_out()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputFolderError("The output folder can't be created: %s (%s)" % (out, exc)) from None
    job = Job(data, out, echo=echo)
    if job_hook:
        job_hook(job)
    job.log("mysql-rescue %s, mode %s" % (data, mode))
    diag = None
    results = {}
    outputs = {}
    attempts = []
    ts = None
    try:
        job.progress("copy", 0, 1)
        job.secure_copy(data)
        job.progress("copy", 1, 1)
        job.progress("diagnose", 0, 1)
        diag = inspect_folder.inspect(job.pristine)
        job.save_json(job.dir / "diagnosis.json", diag)
        job.log("detected %s %s (%s), %d databases" % (diag["family"], diag["version"], diag["generation"],
                                                      len(diag["databases"])))
        for w in diag["warnings"]:
            job.warn(w)
        user_dbs = [d["name"] for d in diag["databases"] if not d["system"]]
        wanted = [d for d in (databases or user_dbs) if d in user_dbs] if databases else user_dbs
        missing = sorted(set(databases or []) - set(user_dbs))
        if missing:
            job.warn("not in this folder: %s" % ", ".join(missing))
        for d in diag["databases"]:
            if d["name"] in wanted:
                results[d["name"]] = {t["name"].lower(): {"table": t["name"], "engine": t["engine"], "rows": None,
                                                          "status": "pending", "level": None, "error": "; ".join(t["problems"])}
                                      for t in d["tables"]}
        job.progress("diagnose", 1, 1)
        if mode == "diagnose":
            for tables in results.values():
                for t in tables.values():
                    t["status"] = "not tried"
            job.results["status"] = "completed"
            return job, _finish(job, diag, results, outputs, attempts)

        found = servers_mod.find_servers(server_dirs(extra_server_dirs))
        candidates = servers_mod.candidates_for(diag["family"], diag["version"], diag["generation"], found)
        if not candidates:
            raise RuntimeError("no server build that can open %s %s data was found (put one into the servers\\ folder "
                               "or name a folder with --servers)" % (diag["family"], diag["version"]))
        job.log("servers to try: %s" % ", ".join(c.label for c in candidates))
        dump_dir = job.dir / "tables"
        extras_done = set()
        for server in candidates:
            started_once = False
            for level in MODES[mode]:
                pending = [(db, key) for db, tables in results.items() for key, t in tables.items()
                           if t["status"] in ("pending", "failed")]
                if not pending:
                    break
                job.progress("start", 0, 1, "%s, %s" % (server.label, FORCE_LEVELS[level]))
                work = job.work_copy("work")
                ts = TempServer(server, work, job.dir, force=level, log=job.log)
                try:
                    ts.start(should_stop=job.stopped)
                except ServerError as exc:
                    job.warn("%s didn't start at level %d: %s" % (server.label, level, exc))
                    attempts.append({"server": server.label, "level": level, "result": "didn't start: %s" % str(exc)[:300]})
                    ts = None
                    continue
                started_once = True
                job.progress("start", 1, 1, "%s is up" % server.label)
                done_here = 0
                crashed = False
                for db in list(results):
                    if crashed:
                        break
                    try:
                        listed = _list_tables(ts, db)
                    except ServerError as exc:
                        job.warn("can't list the tables of %s: %s" % (db, exc))
                        if not ts.alive():
                            crashed = True
                        continue
                    seen = {name.lower(): (name, kind, engine) for name, kind, engine in listed}
                    for key, (name, kind, engine) in seen.items():  # tables only the server knows (e.g. in ibdata1)
                        if kind == "BASE TABLE" and key not in results[db]:
                            results[db][key] = {"table": name, "engine": engine, "rows": None, "status": "pending",
                                                "level": None, "error": ""}
                    views = [name for name, kind, _e in listed if kind == "VIEW"]
                    for key, t in results[db].items():
                        if key in seen and seen[key][1] == "VIEW":
                            t["status"], t["engine"] = "view", "VIEW"
                    todo = [(key, t) for key, t in sorted(results[db].items()) if t["status"] in ("pending", "failed")]
                    total = len(todo)
                    for i, (key, t) in enumerate(todo):
                        job.progress("dump", i, total, "%s.%s" % (db, t["table"]))
                        if key not in seen:
                            t["status"], t["error"] = "failed", "the server doesn't see this table (its definition may be lost)"
                            continue
                        name, _kind, engine = seen[key]
                        t["engine"] = engine or t["engine"]
                        target = dump_dir / db / ("%s.sql" % name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            count = ts.query("SELECT COUNT(*) FROM `%s`.`%s`" % (db.replace("`", "``"), name.replace("`", "``")))
                            ts.dump_table(db, name, target)
                            t.update(status="ok", rows=int(count[0][0]) if count else None, level=level, error="",
                                     server=server.label)
                            done_here += 1
                        except ServerError as exc:
                            t.update(status="failed", error=str(exc)[:500], level=level)
                            target.unlink(missing_ok=True)
                            job.warn("%s.%s at level %d: %s" % (db, name, level, exc))
                            if not ts.alive():
                                job.warn("the server crashed on %s.%s; starting again with the next recovery level" % (db, name))
                                crashed = True
                                break
                    if not crashed and db not in extras_done:
                        try:
                            base = [t["table"] for t in results[db].values() if t["status"] == "ok"]
                            (dump_dir / db).mkdir(parents=True, exist_ok=True)  # also for a database without tables
                            ts.dump_extras(db, base, views, dump_dir / db / "_views_routines.sql")
                            extras_done.add(db)
                        except ServerError as exc:
                            job.warn("views / routines of %s: %s" % (db, exc))
                attempts.append({"server": server.label, "level": level,
                                 "result": "%d tables dumped%s" % (done_here, ", then the server crashed" if crashed else "")})
                ts.stop()
                ts = None
            left = [1 for tables in results.values() for t in tables.values() if t["status"] in ("pending", "failed")]
            if not left or (started_once and len(candidates) == 1):
                break
        job.progress("combine", 0, 1)
        for db, tables in results.items():
            ok = [t for t in sorted(tables.values(), key=lambda t: t["table"].lower()) if t["status"] == "ok"]
            if not ok:
                continue
            target = job.dir / ("%s.sql" % db)
            with open(target, "wb") as outf:
                outf.write(("-- mysql-rescue: database %s from %s\n-- import with: mysql -u USER -p NEW_DB < %s.sql\n"
                            "SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\n\n" % (db, data, db)).encode("utf-8"))
                for t in ok:
                    with open(dump_dir / db / ("%s.sql" % t["table"]), "rb") as src:
                        outf.write(src.read())
                    outf.write(b"\n")
                extras = dump_dir / db / "_views_routines.sql"
                if extras.is_file():
                    outf.write(extras.read_bytes())
                outf.write(b"\nSET FOREIGN_KEY_CHECKS=1;\nSET UNIQUE_CHECKS=1;\n")
            outputs["sql:%s" % db] = str(target)
        job.progress("combine", 1, 1)
        failed = sum(1 for tables in results.values() for t in tables.values() if t["status"] in ("pending", "failed"))
        for tables in results.values():
            for t in tables.values():
                if t["status"] == "pending":
                    t["status"] = "failed"
                    t["error"] = t["error"] or "no server start could read it"
        job.results["status"] = "completed" if not failed else ("partial" if outputs else "failed")
    except Cancelled:
        job.results["status"] = "stopped"
        job.warn("stopped by user; writing a partial report")
    except Exception as exc:  # noqa: BLE001 - every run ends with a report
        job.results["status"] = "failed"
        job.error("fatal: %s" % exc)
        import traceback
        job.log(traceback.format_exc(), "ERROR")
    finally:
        job.cancel_event = None  # the report must always be written
        if ts is not None:
            ts.stop()
    return job, _finish(job, diag, results, outputs, attempts)


def _finish(job, diag, results, outputs, attempts):
    job.progress("report", 0, 1)
    work = job.dir / "work"
    if work.exists():
        remove_tree(work)  # the last attempt's copy; the pristine copy stays in source\
    if job.manifest is not None:
        job.verify_original_unchanged()
    rpt = report_mod.build(job, diag, results, outputs, attempts)
    rpt["status"] = job.results.get("status")
    job.save_json(job.dir / "report.json", rpt)
    report_mod.write_html(rpt, job.dir / "report.html")
    t = rpt["totals"]
    job.log("TOTAL: %d/%d tables recovered (%s%%), %d rows, status %s" % (t["tables_ok"], t["tables"], t["percent"],
                                                                         t["rows"], rpt["status"]))
    job.log("job folder: %s" % job.dir)
    job.progress("report", 1, 1)
    return rpt


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mysql-rescue", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(MODES))
    ap.add_argument("folder", help="the damaged data folder (or the server folder that contains data\\)")
    ap.add_argument("--out", help="folder for job folders (default: ./jobs next to this tool)")
    ap.add_argument("--db", action="append", help="database to rescue (repeat; default: all)")
    ap.add_argument("--servers", action="append", default=[], help="folder with MySQL/MariaDB builds (repeat)")
    a = ap.parse_args(argv)
    if not Path(a.folder).is_dir():
        print("folder not found: %s" % a.folder, file=sys.stderr)
        return 2
    started = time.time()
    try:
        _job, rpt = run_pipeline(a.folder, a.command, a.out, a.db, a.servers)
    except (inspect_folder.NotADataFolder, OutputFolderError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("done in %.0f s: %s" % (time.time() - started, rpt["status"]))
    return 0 if rpt["status"] in ("completed",) else 1


if __name__ == "__main__":
    sys.exit(main())
