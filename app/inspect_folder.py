"""Read a MySQL / MariaDB data folder without starting anything: what is it, what's in it,
and what looks damaged. Nothing is opened for writing.
"""
import os
import re
from pathlib import Path

SYSTEM_DBS = {"mysql", "performance_schema", "information_schema", "sys", "phpmyadmin"}
INTERNAL_DIRS = {"#innodb_redo", "#innodb_temp", "#mysql50#lost+found", "lost+found"}
TABLE_FILES = {".frm": "definition", ".ibd": "innodb", ".myd": "myisam", ".myi": "myisam-index",
               ".mad": "aria", ".mai": "aria-index", ".csv": "csv", ".csm": "csv-meta", ".arz": "archive",
               ".par": "partitions", ".sdi": "sdi", ".isl": "innodb-link", ".opt": None, ".trg": None, ".trn": None}
PAGE = 16384


class NotADataFolder(Exception):
    pass


def looks_like_data(path):
    """A data folder has ibdata1 / mysql.ibd, or a mysql\\ schema folder holding system tables
    (a server *install* folder also has a mysql\\ or bin\\, but no table files in it)."""
    p = Path(path)
    if (p / "ibdata1").is_file() or (p / "mysql.ibd").is_file():
        return True
    schema = p / "mysql"
    if not schema.is_dir():
        return False
    try:
        return any(f.suffix.lower() in (".frm", ".myi", ".myd", ".ibd", ".mai", ".mad") for f in schema.iterdir())
    except OSError:
        return False


def find_data_dir(path):
    """The data folder itself, or the data\\ inside a picked server folder (e.g. WAMP's mysql5.6.12)."""
    p = Path(path)
    if looks_like_data(p):
        return p
    for sub in ("data", "Data", "mysql/data"):
        if looks_like_data(p / sub):
            return p / sub
    raise NotADataFolder("%s doesn't look like a MySQL/MariaDB data folder (no mysql\\ folder or ibdata1)" % p)


def detect(path):
    """(family, version, generation): family mysql|mariadb, version text ('' if unknown),
    generation '5.x' | '8.x' | 'mariadb'."""
    p = Path(path)
    version = ""
    for info in ("mariadb_upgrade_info", "mysql_upgrade_info"):
        f = p / info
        if f.is_file():
            try:
                version = f.read_bytes().decode("utf-8", "replace").strip("\x00 \r\n")
            except OSError:
                pass
            if version:
                break
    if (p / "aria_log_control").is_file() or "mariadb" in version.lower():
        return "mariadb", version, "mariadb"
    if (p / "mysql.ibd").is_file() or (p / "#innodb_redo").is_dir() or (p / "undo_001").is_file():
        return "mysql", version or "8.x", "8.x"
    if (p / "sys").is_dir() and not version:
        version = "5.7"
    return "mysql", version or "5.6", "5.x"


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return -1


def inspect(path):
    """Everything the window and the report show before any server runs."""
    data = find_data_dir(path)
    family, version, generation = detect(data)
    warnings = []
    shared = {}
    for name in ("ibdata1", "ib_logfile0", "ib_logfile1", "mysql.ibd", "undo_001", "undo_002", "aria_log_control"):
        f = data / name
        if f.exists():
            shared[name] = _size(f)
    if generation != "8.x" and "ibdata1" not in shared:
        warnings.append("ibdata1 is missing: InnoDB tables can't be opened normally (their .ibd files may still hold the rows)")
    elif shared.get("ibdata1") == 0:
        warnings.append("ibdata1 is empty (0 bytes)")
    elif shared.get("ibdata1", PAGE) % PAGE:
        warnings.append("ibdata1 is not a whole number of 16 KB pages: it may be cut off")
    logs = [shared[n] for n in ("ib_logfile0", "ib_logfile1") if n in shared]
    if len(logs) == 2 and logs[0] != logs[1] and family == "mysql":
        warnings.append("ib_logfile0 and ib_logfile1 have different sizes: the redo log is probably damaged")
    if generation == "8.x" and "mysql.ibd" not in shared:
        warnings.append("mysql.ibd (MySQL 8's data dictionary) is missing: tables can only be rebuilt from their .ibd files")
    databases = []
    for entry in sorted(data.iterdir(), key=lambda e: e.name.lower()):
        if not entry.is_dir() or entry.name.lower() in INTERNAL_DIRS or entry.name.startswith("#"):
            continue
        tables = {}
        for f in sorted(entry.iterdir(), key=lambda e: e.name.lower()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in TABLE_FILES or TABLE_FILES[ext] is None:
                continue
            stem = f.stem
            if "#p#" in stem.lower():  # partition files belong to their table
                stem = re.split(r"#[pP]#", stem)[0]
            t = tables.setdefault(stem, {"name": stem, "files": {}, "size": 0})
            t["files"][ext] = _size(f)
            t["size"] += max(_size(f), 0)
        rows = []
        for t in tables.values():
            files = t["files"]
            if ".ibd" in files:
                engine = "InnoDB"
            elif ".myd" in files or ".myi" in files:
                engine = "MyISAM"
            elif ".mad" in files or ".mai" in files:
                engine = "Aria"
            elif ".csv" in files:
                engine = "CSV"
            elif ".arz" in files:
                engine = "ARCHIVE"
            elif ".frm" in files:
                engine = "InnoDB (shared)" if "ibdata1" in shared else "?"  # rows live in ibdata1
            else:
                engine = "?"
            problems = []
            if generation != "8.x" and ".frm" not in files and engine not in ("?",):
                problems.append("definition (.frm) missing")
            for ext, size in files.items():
                if size == 0 and ext in (".frm", ".ibd", ".myi", ".mai"):
                    problems.append("%s is empty" % ext)
                elif ext == ".ibd" and size > 0 and size % PAGE:
                    problems.append(".ibd is not a whole number of pages")
            if engine == "MyISAM" and (".myd" not in files or ".myi" not in files):
                problems.append("MyISAM table without its %s" % (".MYD" if ".myd" not in files else ".MYI"))
            rows.append({"name": t["name"], "engine": engine, "size": t["size"], "files": sorted(files),
                         "problems": problems})
        system = entry.name.lower() in SYSTEM_DBS
        databases.append({"name": entry.name, "system": system, "tables": rows,
                          "size": sum(r["size"] for r in rows),
                          "problems": sum(1 for r in rows if r["problems"])})
    user = [d for d in databases if not d["system"]]
    if not user:
        warnings.append("no user databases found in this folder")
    return {"data_dir": str(data), "family": family, "version": version, "generation": generation,
            "shared": shared, "databases": databases, "warnings": warnings,
            "size": sum(_size(Path(dp) / f) for dp, _d, fs in os.walk(data) for f in fs)}
