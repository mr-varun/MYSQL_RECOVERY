"""A temporary MySQL / MariaDB server on a COPY of the damaged data folder.

It listens on loopback only (a free port, or a private named pipe for MySQL 8, which turns
networking off under skip-grant-tables), with no passwords (skip-grant-tables), no binlog, no
replication and no event scheduler, so nothing runs on its own. innodb_force_recovery is set
when asked for. It never sees the original folder.
"""
import os
import socket
import subprocess
import time
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FORCE_LEVELS = {
    0: "normal start (crash recovery as usual)",
    1: "ignore corrupt pages (SRV_FORCE_IGNORE_CORRUPT)",
    2: "no background threads (SRV_FORCE_NO_BACKGROUND)",
    3: "no transaction rollback (SRV_FORCE_NO_TRX_UNDO)",
    4: "no insert-buffer merge (SRV_FORCE_NO_IBUF_MERGE)",
    5: "ignore undo logs (SRV_FORCE_NO_UNDO_LOG_SCAN)",
    6: "skip the redo log (SRV_FORCE_NO_LOG_REDO)",
}


class ServerError(Exception):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fwd(path):
    return str(path).replace("\\", "/")  # option files treat \ as an escape character


class TempServer:
    def __init__(self, server, datadir, workdir, force=0, log=print):
        self.server, self.datadir, self.workdir, self.force, self.log = server, Path(datadir), Path(workdir), force, log
        self.port = free_port()
        self.pipe = "mysqlrescue%d" % self.port
        self.mysql8 = server.family == "mysql" and server.major_minor >= (8, 0)
        self.error_log = self.workdir / ("server-level%d.err" % force)
        self.ini = self.workdir / ("my-level%d.ini" % force)
        self.proc = None

    # ------------------------------------------------------------------ start / stop
    def _write_ini(self):
        s = self.server
        lines = ["[mysqld]", "basedir=%s" % fwd(s.home), "datadir=%s" % fwd(self.datadir), "port=%d" % self.port,
                 "bind-address=127.0.0.1", "skip-grant-tables", "skip-name-resolve", "event_scheduler=OFF",
                 "log-error=%s" % fwd(self.error_log), "innodb_buffer_pool_size=256M", "max_allowed_packet=1G",
                 "tmpdir=%s" % fwd(self.workdir)]
        if self.force:
            lines.append("innodb_force_recovery=%d" % self.force)
        if self.mysql8:
            lines += ["named_pipe=ON", "socket=%s" % self.pipe, "mysqlx=OFF", "skip_replica_start", "disable-log-bin"]
        else:
            lines += ["skip-slave-start"]
        if s.family == "mariadb":
            lines += ["skip-log-bin"]
        self.ini.write_text("\n".join(lines) + "\n", encoding="ascii")

    def conn_args(self):
        if self.mysql8:
            return ["--protocol=PIPE", "--socket=%s" % self.pipe, "-uroot"]
        return ["--protocol=TCP", "-h127.0.0.1", "-P%d" % self.port, "-uroot"]

    def start(self, timeout=180, should_stop=lambda: False):
        """Start and wait until it answers. Raises ServerError with the end of its error log."""
        self._write_ini()
        self.error_log.unlink(missing_ok=True)
        self.log("starting %s on the copy, %s" % (self.server.label, FORCE_LEVELS.get(self.force, "level %d" % self.force)))
        self.proc = subprocess.Popen([str(self.server.exe), "--defaults-file=%s" % self.ini], cwd=str(self.workdir),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ServerError("the server stopped while starting (exit %s): %s" % (self.proc.returncode, self.error_tail()))
            if should_stop():
                self.stop()
                raise KeyboardInterrupt()
            if self.query_ok("SELECT 1"):
                self.log("server is up (%s)" % ("named pipe" if self.mysql8 else "port %d" % self.port))
                return
            time.sleep(0.7)
        self.kill()
        raise ServerError("the server didn't answer within %d s: %s" % (timeout, self.error_tail()))

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=60):
        if not self.alive():
            return
        admin = self.server.admin
        if admin:
            try:
                subprocess.run([str(admin), "--no-defaults", *self.conn_args(), "shutdown"], capture_output=True,
                               timeout=30, creationflags=CREATE_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self):
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                pass

    def error_tail(self, lines=8):
        try:
            text = self.error_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(no error log)"
        important = [l for l in text if "[ERROR]" in l or "Error" in l or "error" in l]
        return " | ".join((important or text)[-lines:])[-1500:]

    # ------------------------------------------------------------------ queries
    def _client(self, sql, timeout):
        return subprocess.run([str(self.server.client), "--no-defaults", *self.conn_args(), "-N", "-B",
                               "--default-character-set=utf8mb4", "-e", sql], capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)

    def query_ok(self, sql):
        try:
            return self._client(sql, 15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def query(self, sql, timeout=600):
        """Rows as lists of strings. Raises ServerError (and notes whether the server crashed)."""
        try:
            result = self._client(sql, timeout)
        except subprocess.TimeoutExpired:
            raise ServerError("query timed out after %d s" % timeout) from None
        if result.returncode != 0:
            raise ServerError(self._failure(result.stderr))
        text = result.stdout.decode("utf-8", "replace")
        return [line.split("\t") for line in text.splitlines() if line]

    def _failure(self, stderr):
        msg = stderr.decode("utf-8", "replace").strip().splitlines()
        msg = msg[-1] if msg else "failed"
        if not self.alive():
            msg += " (the server crashed: %s)" % self.error_tail(4)
        return msg

    def dump_table(self, database, table, out_file, timeout=3600):
        """mysqldump of one table (structure + rows) into out_file."""
        args = [str(self.server.dump), "--no-defaults", *self.conn_args(), "--skip-lock-tables", "--quick",
                "--hex-blob", "--skip-triggers", "--no-tablespaces", "--default-character-set=utf8mb4",
                "--skip-comments", "--result-file=%s" % out_file]
        if self.mysql8:
            args.append("--set-gtid-purged=OFF")
        args += [database, table]
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ServerError("the dump timed out after %d s" % timeout) from None
        if result.returncode != 0:
            raise ServerError(self._failure(result.stderr))

    def dump_extras(self, database, tables, views, out_file, timeout=600):
        """Views, triggers, routines and events of one database (no table rows)."""
        args = [str(self.server.dump), "--no-defaults", *self.conn_args(), "--skip-lock-tables", "--no-data",
                "--no-create-info", "--routines", "--events", "--triggers", "--no-tablespaces", "--skip-comments",
                "--default-character-set=utf8mb4", "--result-file=%s" % out_file]
        if self.mysql8:
            args.append("--set-gtid-purged=OFF")
        args += [database, *tables]
        result = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise ServerError(self._failure(result.stderr))
        if views:
            vfile = Path(str(out_file) + ".views")
            vargs = [str(self.server.dump), "--no-defaults", *self.conn_args(), "--skip-lock-tables", "--no-data",
                     "--skip-comments", "--no-tablespaces", "--default-character-set=utf8mb4",
                     "--result-file=%s" % vfile, database, *views]
            if self.mysql8:
                vargs.insert(-1 - len(views), "--set-gtid-purged=OFF")
            result = subprocess.run(vargs, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
            if result.returncode == 0:
                with open(out_file, "ab") as out, open(vfile, "rb") as src:
                    out.write(src.read())
            os.remove(vfile) if vfile.exists() else None
