"""Job folder handling: the safe copy of the data folder, logging, progress and stop.

Rules enforced here:
  * the damaged data folder is only ever read (files opened 'rb', nothing created next to it);
  * all work happens on copies inside job-<timestamp>-<name>\\ ;
  * a manifest (size + modification time of every file) is taken before copying and checked again
    at the end, so the report can say the original was not touched.
"""
import datetime
import json
import os
import shutil
import stat
import threading
from pathlib import Path

CHUNK = 4 * 1024 * 1024
# never copied: server leftovers that would only confuse a temporary server
SKIP_FILES = {"mysql.sock", "mysqld.pid", "aria_log.lock"}
SKIP_SUFFIXES = (".pid", ".lock", ".sock")


class Cancelled(KeyboardInterrupt):
    """Raised at the next progress point after a stop was requested (window Stop / Ctrl+C)."""


def manifest(folder):
    """{relative path: (size, mtime_ns)} of every file under `folder`."""
    out = {}
    root = Path(folder)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def folder_size(folder):
    return sum(size for size, _ in manifest(folder).values())


class Job:
    def __init__(self, source, out_root, echo=True):
        self.source = Path(source).resolve()
        root = Path(out_root)
        root.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = self.source.name if self.source.name.lower() != "data" else self.source.parent.name + "-data"
        base = root / ("job-%s-%s" % (ts, name))
        d = base
        k = 2
        while d.exists():
            d = Path("%s-%d" % (base, k))
            k += 1
        d.mkdir()
        self.dir = d
        self.echo = echo
        self._lock = threading.Lock()
        self.log_path = d / "log.txt"
        self.listeners = []            # fn(line) for every log line
        self.progress_listeners = []   # fn(stage, done, total, note)
        self.cancel_event = None       # threading.Event set by a window "Stop"
        self.results = {"source": str(self.source), "job_dir": str(d),
                        "started": datetime.datetime.now().isoformat(timespec="seconds"), "stages": {}}
        self.pristine = None           # the copy every attempt starts from (read-only)
        self.manifest = None

    # ------------------------------------------------------------- logging
    def log(self, msg, level="INFO"):
        line = "%s %-5s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), level, msg)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.echo:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode(), flush=True)
        for fn in self.listeners:
            fn(line)

    def warn(self, msg):
        self.log(msg, "WARN")

    def error(self, msg):
        self.log(msg, "ERROR")

    def progress(self, stage, done, total, note=""):
        """Report progress of a stage; also the point where a requested stop takes effect."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled()
        for fn in self.progress_listeners:
            fn(stage, done, total, note)

    def stopped(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    # ------------------------------------------------------------- copies
    def secure_copy(self, data_dir):
        """Copy the data folder into job\\source\\ (read-only), remembering a manifest of the original."""
        src = Path(data_dir)
        self.manifest = manifest(src)
        total = sum(size for size, _ in self.manifest.values())
        dst = self.dir / "source"
        self.log("copying %s (%d files, %.1f MB) to %s" % (src, len(self.manifest), total / 1048576, dst))
        done = 0
        for rel in sorted(self.manifest):
            name = Path(rel).name
            if name.lower() in SKIP_FILES or name.lower().endswith(SKIP_SUFFIXES):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(src / rel, "rb") as fi, open(target, "xb") as fo:
                while True:
                    block = fi.read(CHUNK)
                    if not block:
                        break
                    fo.write(block)
                    done += len(block)
                    self.progress("copy", done, total or 1, "copying the data folder")
            os.chmod(target, stat.S_IREAD)
        self.pristine = dst
        self.results["size"] = total
        self.results["files"] = len(self.manifest)
        self.log("copied %d files, %.1f MB" % (len(self.manifest), total / 1048576))
        return dst

    def work_copy(self, name):
        """A fresh writable copy of the pristine copy, for one server start attempt."""
        dst = self.dir / name
        if dst.exists():
            remove_tree(dst)
        shutil.copytree(self.pristine, dst)
        for dirpath, _dirs, files in os.walk(dst):
            for f in files:
                os.chmod(Path(dirpath) / f, stat.S_IREAD | stat.S_IWRITE)
        return dst

    def verify_original_unchanged(self):
        now = manifest(self.source)
        same = now == self.manifest
        self.results["original_unchanged"] = same
        if same:
            self.log("original folder re-checked: unchanged (%d files)" % len(now))
        else:
            changed = sorted(k for k in set(now) | set(self.manifest) if now.get(k) != self.manifest.get(k))
            self.error("the original folder CHANGED while the rescue ran (is a server using it?): %s"
                       % ", ".join(changed[:10]))
        return same

    def save_json(self, path, obj):
        Path(path).write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def remove_tree(path):
    def writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=writable)
