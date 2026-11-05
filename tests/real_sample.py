"""Real-world test: rescue a real data folder, then damaged COPIES of it, and compare with the baseline.

    python tests/real_sample.py "D:\\path\\to\\data"            (all damages)
    python tests/real_sample.py "D:\\path\\to\\data" bad_page   (just one)

The folder is only read (it's copied, and its manifest is checked again at the end).
Results: tests/work/real/<damage>/jobs and tests/work/real/real_sample.json.
"""
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
sys.path.insert(0, str(HERE))
import corruptor  # noqa: E402
import inspect_folder  # noqa: E402
import mysql_rescue  # noqa: E402
from job import manifest  # noqa: E402

WORK = HERE / "work" / "real"
PAGE = 16384


def biggest_innodb_table(data):
    best = None
    for db in sorted(p for p in data.iterdir() if p.is_dir() and p.name.lower() not in inspect_folder.SYSTEM_DBS):
        for ibd in db.glob("*.ibd"):
            if best is None or ibd.stat().st_size > best.stat().st_size:
                best = ibd
    return best


def damage(name, data):
    """Damage the copy `data` in place; returns a description."""
    if name == "healthy":
        return "untouched copy"
    if name == "bad_page":
        ibd = biggest_innodb_table(data)
        pages = ibd.stat().st_size // PAGE
        corruptor._garble(ibd, (pages // 2) * PAGE, PAGE)
        return "one garbage page in %s/%s (%d pages)" % (ibd.parent.name, ibd.name, pages)
    if name == "torn_redo":
        log = data / "ib_logfile0"
        corruptor._garble(log, 2048, 256 * 1024)
        return "ib_logfile0 log blocks overwritten"
    if name == "no_ibdata":
        (data / "ibdata1").unlink()
        return "ibdata1 deleted (phase 3: rebuild from .frm + .ibd)"
    raise ValueError(name)


DAMAGES = ["healthy", "bad_page", "torn_redo", "no_ibdata"]


def counts(rpt):
    return {(d["database"], t["table"]): (t["rows"] if t["status"] == "ok" else None)
            for d in rpt["databases"] for t in d["tables"]}


def main(folder, only=None):
    source = inspect_folder.find_data_dir(folder)
    before = manifest(source)
    WORK.mkdir(parents=True, exist_ok=True)
    baseline = None
    board = []
    for name in DAMAGES:
        if only and name not in only and name != "healthy":
            continue
        case = WORK / name
        if case.exists():
            corruptor.remove(case)
        started = time.monotonic()
        data = case / "data"
        shutil.copytree(source, data)
        what = damage(name, data)
        _job, rpt = mysql_rescue.run_pipeline(data, "recover", out=case / "jobs", echo=False)
        got = counts(rpt)
        if name == "healthy":
            baseline = got
        lost_tables = sorted("%s.%s" % k for k, v in got.items() if v is None)
        rows_base = sum(v or 0 for v in baseline.values()) if baseline else 0
        rows_got = sum(v or 0 for v in got.values())
        differ = sorted("%s.%s %s->%s" % (k[0], k[1], baseline.get(k), v) for k, v in got.items()
                        if baseline and v is not None and baseline.get(k) not in (None, v))
        t = rpt["totals"]
        row = {"damage": name, "what": what, "status": rpt["status"], "tables_ok": t["tables_ok"], "tables": t["tables"],
               "rows": rows_got, "baseline_rows": rows_base, "level": t["highest_level"], "lost_tables": lost_tables[:20],
               "changed_counts": differ[:20], "attempts": rpt["attempts"], "seconds": round(time.monotonic() - started, 1)}
        board.append(row)
        print("%-10s %-9s %4d/%-4d tables  %8d/%-8d rows  level %s  %5.0f s  %s" % (
            name, rpt["status"], t["tables_ok"], t["tables"], rows_got, rows_base, t["highest_level"], row["seconds"], what))
        for item in lost_tables[:5]:
            print("           not recovered: %s" % item)
        for item in differ[:5]:
            print("           row count differs: %s" % item)
        corruptor.remove(data)  # the damaged copy is big; the job folder keeps its own copy
    (WORK / "real_sample.json").write_text(json.dumps(board, indent=2), encoding="utf-8")
    print("original folder unchanged:", manifest(source) == before)
    return board


if __name__ == "__main__":
    main(sys.argv[1], set(sys.argv[2:]) or None)
