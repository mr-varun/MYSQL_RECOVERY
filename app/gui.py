"""mysql-rescue window: pick a damaged MySQL / MariaDB data folder, choose what you need, start.

Layout: left = what you choose (folder, databases, mode, output folder, Start / Stop);
right = what happens (stages, progress, log) and the results, plus the list of earlier jobs with
Browse / Clean (keep the rescued files) / Full clean. The window runs the same pipeline as the
command line (mysql_rescue.run_pipeline) on a worker thread.
"""
import json
import os
import queue
import shutil
import stat
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inspect_folder  # noqa: E402
import mysql_rescue  # noqa: E402

ROOT = HERE.parent
SETTINGS = ROOT / "settings.json"
MODES = [("recover", "Recover my databases", "Recommended: a normal start first, then recovery levels 1-6 only if needed"),
         ("force", "Server won't start at all", "Straight to forced recovery (innodb_force_recovery 1-6)"),
         ("diagnose", "Just diagnose", "Reads the folder only and explains what it finds; nothing is recovered")]
STAGES = ["copy", "diagnose", "start", "dump", "combine", "report"]
WORKING_DIRS = ("source", "work", "tables")  # Clean removes these; the rescued files at the top stay
C = dict(bg="#f2f4f7", card="#ffffff", line="#d8dce3", fg="#1b1f27", mut="#677083", acc="#2f6fed", ok="#11804a",
         bad="#c0362c", warn="#a86b00")


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(changes):
    data = load_settings()
    data.update(changes)
    SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")


def tree_size(path):
    total = 0
    for dirpath, _d, files in os.walk(path):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total


def remove_tree(path):
    def writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)  # the copy of the data folder is read-only
        func(p)
    shutil.rmtree(path, onerror=writable)


def mb(n):
    return "%.1f MB" % (n / 1048576) if n < 1024 ** 3 else "%.2f GB" % (n / 1024 ** 3)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop_event = None
        self.worker = None
        self.diag = None
        self.db_vars = {}
        self.job_dir = None
        settings = load_settings()
        root.title("mysql-rescue: recover a damaged MySQL / MariaDB data folder")
        root.geometry("1220x780")
        root.minsize(980, 640)
        root.configure(bg=C["bg"])
        self._style()
        self.folder = tk.StringVar(value=settings.get("last_folder", ""))
        self.out = tk.StringVar(value=settings.get("out") or str(mysql_rescue.default_out()))
        self.mode = tk.StringVar(value="recover")
        self._build()
        self.root.after(100, self._poll)
        self.load_jobs()
        if self.folder.get() and Path(self.folder.get()).is_dir():
            self.scan()

    # ------------------------------------------------------------------ layout
    def _style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=C["bg"], foreground=C["fg"], font=("Segoe UI", 10))
        st.configure("Card.TFrame", background=C["card"])
        st.configure("Card.TLabel", background=C["card"], foreground=C["fg"])
        st.configure("Mut.TLabel", background=C["card"], foreground=C["mut"], font=("Segoe UI", 9))
        st.configure("H.TLabel", background=C["card"], foreground=C["fg"], font=("Segoe UI Semibold", 12))
        st.configure("Title.TLabel", background=C["bg"], foreground=C["fg"], font=("Segoe UI Semibold", 16))
        st.configure("TRadiobutton", background=C["card"], foreground=C["fg"])
        st.configure("TCheckbutton", background=C["card"], foreground=C["fg"])
        st.configure("Accent.TButton", background=C["acc"], foreground="#ffffff", font=("Segoe UI Semibold", 10), padding=(14, 7))
        st.map("Accent.TButton", background=[("disabled", "#9db7ef"), ("active", "#245bd0")])
        st.configure("Treeview", rowheight=24, fieldbackground=C["card"], background=C["card"])
        st.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def card(self, parent):
        f = ttk.Frame(parent, style="Card.TFrame", padding=12)
        f.configure(borderwidth=1, relief="solid")
        return f

    def _build(self):
        top = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        top.pack(fill="x")
        ttk.Label(top, text="mysql-rescue", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  the original folder is only read: everything happens on a copy",
                  foreground=C["mut"], background=C["bg"]).pack(side="left")
        ttk.Button(top, text="? Guide", command=self.open_guide).pack(side="right")
        body = ttk.Frame(self.root, padding=(14, 4, 14, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        # 1. folder
        c1 = self.card(left)
        c1.pack(fill="x", pady=(0, 10))
        ttk.Label(c1, text="1  The damaged data folder", style="H.TLabel").pack(anchor="w")
        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x", pady=6)
        ttk.Entry(row, textvariable=self.folder, width=46).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left", padx=(6, 0))
        self.info = ttk.Label(c1, text="Choose the data folder (or the server folder that contains data\\).",
                              style="Mut.TLabel", wraplength=400, justify="left")
        self.info.pack(anchor="w")
        ttk.Label(c1, text="Databases", style="Mut.TLabel").pack(anchor="w", pady=(8, 2))
        self.db_box = ttk.Frame(c1, style="Card.TFrame")
        self.db_box.pack(fill="x")

        # 2. mode
        c2 = self.card(left)
        c2.pack(fill="x", pady=(0, 10))
        ttk.Label(c2, text="2  What do you need?", style="H.TLabel").pack(anchor="w", pady=(0, 4))
        for key, title, hint in MODES:
            ttk.Radiobutton(c2, text=title, value=key, variable=self.mode).pack(anchor="w")
            ttk.Label(c2, text="     " + hint, style="Mut.TLabel", wraplength=400).pack(anchor="w", pady=(0, 4))

        # 3. output + start
        c3 = self.card(left)
        c3.pack(fill="x")
        ttk.Label(c3, text="Results go to", style="Mut.TLabel").pack(anchor="w")
        row = ttk.Frame(c3, style="Card.TFrame")
        row.pack(fill="x", pady=(2, 10))
        ttk.Entry(row, textvariable=self.out, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Change...", command=self.change_out).pack(side="left", padx=(6, 0))
        row = ttk.Frame(c3, style="Card.TFrame")
        row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="▶  Start rescue", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row, text="■  Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.status = ttk.Label(c3, text="", style="Mut.TLabel", wraplength=400)
        self.status.pack(anchor="w", pady=(8, 0))

        # right: progress, log, results, jobs
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        run = ttk.Frame(nb, padding=10)
        nb.add(run, text="Progress")
        self.stage_labels = {}
        stages = ttk.Frame(run)
        stages.pack(fill="x")
        for i, s in enumerate(STAGES):
            lbl = ttk.Label(stages, text="○  " + mysql_rescue.STAGE_NAMES[s], foreground=C["mut"])
            lbl.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 24), pady=2)
            self.stage_labels[s] = lbl
        self.bar = ttk.Progressbar(run, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(8, 2))
        self.note = ttk.Label(run, text="", foreground=C["mut"])
        self.note.pack(anchor="w")
        logf = ttk.Frame(run)
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(logf, height=12, wrap="word", bg="#0f1115", fg="#4ade80", insertbackground="#ffffff",
                           font=("Consolas", 9), relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        res = ttk.Frame(nb, padding=10)
        nb.add(res, text="Result")
        self.summary = ttk.Label(res, text="Run a rescue to see what came back.", font=("Segoe UI Semibold", 11))
        self.summary.pack(anchor="w")
        cols = ("database", "table", "engine", "rows", "status", "level", "note")
        self.tree = ttk.Treeview(res, columns=cols, show="headings")
        for col, width in zip(cols, (120, 200, 80, 90, 70, 50, 380)):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=width, anchor="e" if col in ("rows", "level") else "w")
        self.tree.tag_configure("failed", foreground=C["bad"])
        self.tree.pack(fill="both", expand=True, pady=8)
        row = ttk.Frame(res)
        row.pack(fill="x")
        ttk.Button(row, text="Open report", command=lambda: self.open_job("report.html")).pack(side="left")
        ttk.Button(row, text="Open job folder", command=lambda: self.open_job("")).pack(side="left", padx=8)

        jobs = ttk.Frame(nb, padding=10)
        nb.add(jobs, text="Jobs")
        self.jobs_info = ttk.Label(jobs, text="", foreground=C["mut"])
        self.jobs_info.pack(anchor="w")
        cols = ("job", "status", "result", "size", "working")
        self.jobs_tv = ttk.Treeview(jobs, columns=cols, show="headings", selectmode="browse")
        for col, width in zip(cols, (380, 90, 220, 90, 110)):
            self.jobs_tv.heading(col, text={"working": "working files"}.get(col, col.capitalize()))
            self.jobs_tv.column(col, width=width, anchor="e" if col in ("size", "working") else "w")
        self.jobs_tv.pack(fill="both", expand=True, pady=8)
        self.jobs_tv.bind("<Double-1>", lambda _e: self.show_selected_job())
        row = ttk.Frame(jobs)
        row.pack(fill="x")
        ttk.Button(row, text="Show result", command=self.show_selected_job).pack(side="left")
        ttk.Button(row, text="Browse", command=self.browse_job).pack(side="left", padx=8)
        ttk.Button(row, text="Clean (keep rescued files)", command=self.clean_job).pack(side="left")
        ttk.Button(row, text="Full clean", command=self.delete_job).pack(side="left", padx=8)
        ttk.Button(row, text="Refresh", command=self.load_jobs).pack(side="right")

    def open_guide(self):
        guide = ROOT / "GUIDE.md"
        if guide.is_file():
            os.startfile(str(guide))  # noqa: S606 - opens in the default editor / viewer
        else:
            messagebox.showinfo("mysql-rescue", "GUIDE.md is missing from the tool folder.")

    # ------------------------------------------------------------------ folder
    def browse(self):
        d = filedialog.askdirectory(title="The damaged data folder", initialdir=self.folder.get() or str(ROOT))
        if d:
            self.folder.set(os.path.normpath(d))
            self.scan()

    def change_out(self):
        d = filedialog.askdirectory(title="Folder for rescue results", initialdir=self.out.get())
        if d:
            self.out.set(os.path.normpath(d))
            save_settings({"out": self.out.get()})
            self.load_jobs()

    def scan(self):
        for w in self.db_box.winfo_children():
            w.destroy()
        self.db_vars = {}
        try:
            self.diag = inspect_folder.inspect(self.folder.get())
        except (inspect_folder.NotADataFolder, OSError) as exc:
            self.diag = None
            self.info.configure(text=str(exc), foreground=C["bad"])
            return
        d = self.diag
        save_settings({"last_folder": self.folder.get()})
        text = "%s %s (%s) in %s, %s" % ("MariaDB" if d["family"] == "mariadb" else "MySQL", d["version"], d["generation"],
                                        d["data_dir"], mb(d["size"]))
        if d["warnings"]:
            text += "\n⚠ " + "\n⚠ ".join(d["warnings"])
        self.info.configure(text=text, foreground=C["warn"] if d["warnings"] else C["mut"])
        for db in d["databases"]:
            if db["system"]:
                continue
            var = tk.BooleanVar(value=True)
            self.db_vars[db["name"]] = var
            label = "%s  (%d tables, %s%s)" % (db["name"], len(db["tables"]), mb(db["size"]),
                                               ", %d with problems" % db["problems"] if db["problems"] else "")
            ttk.Checkbutton(self.db_box, text=label, variable=var).pack(anchor="w")
        if not self.db_vars:
            ttk.Label(self.db_box, text="(no user databases)", style="Mut.TLabel").pack(anchor="w")

    # ------------------------------------------------------------------ running
    def start(self):
        if not self.diag:
            self.scan()
            if not self.diag:
                return
        dbs = [n for n, v in self.db_vars.items() if v.get()]
        if self.db_vars and not dbs:
            messagebox.showinfo("mysql-rescue", "Tick at least one database.")
            return
        self.clear_log()
        for s, lbl in self.stage_labels.items():
            lbl.configure(text="○  " + mysql_rescue.STAGE_NAMES[s], foreground=C["mut"])
        self.stop_event = threading.Event()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status.configure(text="Running...", foreground=C["acc"])
        self.nb.select(0)
        folder, out, mode = self.folder.get(), self.out.get(), self.mode.get()

        def hook(job):
            job.cancel_event = self.stop_event
            job.listeners.append(lambda line: self.q.put(("log", line)))
            job.progress_listeners.append(lambda st, done, total, note="": self.q.put(("progress", (st, done, total, note))))
            self.q.put(("job", str(job.dir)))

        def work():
            try:
                _job, rpt = mysql_rescue.run_pipeline(folder, mode, out, dbs or None, job_hook=hook, echo=False)
                self.q.put(("done", rpt))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
            self.status.configure(text="Stopping at the next safe point (a report is still written)...", foreground=C["warn"])

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.append_log(data)
                elif kind == "job":
                    self.job_dir = Path(data)
                elif kind == "progress":
                    self.on_progress(*data)
                elif kind == "done":
                    self.on_done(data)
                elif kind == "error":
                    self.append_log("ERROR " + data)
                    self.on_finished("failed: " + data, C["bad"])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_progress(self, stage, done, total, note):
        if stage in self.stage_labels:
            idx = STAGES.index(stage)
            for s in STAGES[:idx]:
                self.stage_labels[s].configure(text="✔  " + mysql_rescue.STAGE_NAMES[s], foreground=C["ok"])
            finished = total and done >= total
            self.stage_labels[stage].configure(text=("✔  " if finished else "◔  ") + mysql_rescue.STAGE_NAMES[stage],
                                               foreground=C["ok"] if finished else C["acc"])
        self.bar["value"] = 100.0 * done / total if total else 0
        self.note.configure(text=note)

    def on_done(self, rpt):
        t = rpt["totals"]
        color = C["ok"] if rpt["status"] == "completed" else C["warn"] if rpt["status"] in ("partial", "stopped") else C["bad"]
        self.on_finished("%s: %d of %d tables, %s rows" % (rpt["status"], t["tables_ok"], t["tables"], f"{t['rows']:,}"), color)
        self.show_report(rpt)
        self.nb.select(1)

    def on_finished(self, text, color):
        self.status.configure(text=text, foreground=color)
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.load_jobs()

    def append_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ results
    def show_report(self, rpt):
        t = rpt["totals"]
        self.summary.configure(text="%s: %d of %d tables recovered (%s%%), %s rows, highest recovery level %s. Original %s."
                               % (rpt.get("status"), t["tables_ok"], t["tables"], t["percent"], f"{t['rows']:,}",
                                  t["highest_level"], "unchanged" if rpt.get("original_unchanged") else "NOT confirmed unchanged"))
        self.tree.delete(*self.tree.get_children())
        for db in rpt["databases"]:
            for row in db["tables"]:
                self.tree.insert("", "end", tags=(row["status"],), values=(
                    db["database"], row["table"], row.get("engine") or "", "" if row.get("rows") is None else f"{row['rows']:,}",
                    row["status"], "" if row.get("level") is None else row["level"], row.get("error") or ""))
        self.job_dir = Path(rpt["job_dir"])

    def open_job(self, name):
        if not self.job_dir or not self.job_dir.exists():
            messagebox.showinfo("mysql-rescue", "No job yet.")
            return
        target = self.job_dir / name if name else self.job_dir
        if target.exists():
            os.startfile(str(target))  # noqa: S606

    # ------------------------------------------------------------------ jobs
    def jobs_root(self):
        return Path(self.out.get() or mysql_rescue.default_out())

    def load_jobs(self):
        self.jobs_tv.delete(*self.jobs_tv.get_children())
        root = self.jobs_root()
        total = working_total = 0
        if root.is_dir():
            for folder in sorted(root.glob("job-*"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    rpt = json.loads((folder / "report.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    rpt = {}
                t = rpt.get("totals") or {}
                size = tree_size(folder)
                working = sum(tree_size(folder / d) for d in WORKING_DIRS if (folder / d).is_dir())
                total += size
                working_total += working
                result = "%s/%s tables, %s rows" % (t.get("tables_ok", "?"), t.get("tables", "?"), f"{t.get('rows', 0):,}") if t else ""
                self.jobs_tv.insert("", "end", iid=str(folder), values=(folder.name, rpt.get("status", "running?"), result,
                                                                       mb(size), mb(working) if working else "-"))
        self.jobs_info.configure(text="%d jobs in %s use %s, of which %s are working files (Clean removes them). "
                                 "Nothing is deleted unless you choose it." % (len(self.jobs_tv.get_children()), root, mb(total),
                                                                              mb(working_total)))

    def selected_job(self):
        sel = self.jobs_tv.selection()
        if not sel:
            messagebox.showinfo("mysql-rescue", "Select a job first.")
            return None
        folder = Path(sel[0])
        if self.worker and self.worker.is_alive() and self.job_dir and folder == self.job_dir:
            messagebox.showinfo("mysql-rescue", "This job is still running.")
            return None
        return folder

    def show_selected_job(self):
        folder = self.selected_job()
        if folder:
            try:
                self.show_report(json.loads((folder / "report.json").read_text(encoding="utf-8")))
                self.nb.select(1)
            except (OSError, ValueError):
                messagebox.showinfo("mysql-rescue", "This job has no report.")

    def browse_job(self):
        folder = self.selected_job()
        if folder:
            os.startfile(str(folder))  # noqa: S606

    def clean_job(self):
        folder = self.selected_job()
        if not folder:
            return
        freed = 0
        for d in WORKING_DIRS:
            if (folder / d).is_dir():
                freed += tree_size(folder / d)
                remove_tree(folder / d)
        messagebox.showinfo("mysql-rescue", "Working files removed (%s freed). The rescued .sql files, report and log stay." % mb(freed)
                            if freed else "Nothing to clean: only the rescued files are left.")
        self.load_jobs()

    def delete_job(self):
        folder = self.selected_job()
        if not folder:
            return
        if messagebox.askyesno("mysql-rescue", "Full clean: delete %s completely, the rescued .sql files too?\n"
                                               "The original data folder is not touched." % folder.name):
            size = tree_size(folder)
            remove_tree(folder)
            messagebox.showinfo("mysql-rescue", "Deleted (%s freed)." % mb(size))
            self.load_jobs()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
