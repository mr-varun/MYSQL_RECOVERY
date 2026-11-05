"""Find MySQL / MariaDB server builds and pick the ones that can open a given data folder.

A build is any folder with bin\\mysqld.exe or bin\\mariadbd.exe. They're searched in the tool's own
servers\\ folder and in the folders given in settings.json ("server_dirs") or with --servers
(e.g. varsha's bin\\mysql and bin\\mariadb). Each build's version is read once from its own
`mysqld --version`.
"""
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cache = {}


class Server:
    def __init__(self, home, exe, family, version):
        self.home, self.exe, self.family, self.version = Path(home), Path(exe), family, version
        self.bin = self.exe.parent

    @property
    def major_minor(self):
        m = re.match(r"(\d+)\.(\d+)", self.version)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def tool(self, *names):
        for name in names:
            p = self.bin / name
            if p.is_file():
                return p
        return None

    @property
    def client(self):
        return self.tool("mariadb.exe", "mysql.exe")

    @property
    def dump(self):
        return self.tool("mariadb-dump.exe", "mysqldump.exe")

    @property
    def admin(self):
        return self.tool("mariadb-admin.exe", "mysqladmin.exe")

    @property
    def label(self):
        return "%s %s" % ("MariaDB" if self.family == "mariadb" else "MySQL", self.version)

    def __repr__(self):
        return "<Server %s at %s>" % (self.label, self.home)


def _version_of(exe):
    key = str(exe).lower()
    if key in _cache:
        return _cache[key]
    text = ""
    try:
        result = subprocess.run([str(exe), "--no-defaults", "--version"], capture_output=True, text=True, timeout=30,
                                creationflags=CREATE_NO_WINDOW, errors="replace")
        text = (result.stdout or result.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    m = re.search(r"Ver\s+(\d+\.\d+\.\d+)", text)
    version = m.group(1) if m else ""
    if not version:  # fall back to the folder name, e.g. mysql-5.6.12-win32
        m = re.search(r"(\d+\.\d+\.\d+)", str(Path(exe).parent.parent.name))
        version = m.group(1) if m else ""
    family = "mariadb" if "mariadb" in text.lower() or "mariadb" in str(exe).lower() else "mysql"
    _cache[key] = (family, version)
    return family, version


def find_servers(extra_dirs=()):
    roots = [ROOT / "servers", *[Path(d) for d in extra_dirs if d]]
    found = {}
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root, *root.iterdir()] if root.is_dir() else []
        for home in candidates:
            if not home.is_dir():
                continue
            for name in ("mariadbd.exe", "mysqld.exe"):
                exe = home / "bin" / name
                if exe.is_file():
                    family, version = _version_of(exe)
                    if version:
                        found[str(home).lower()] = Server(home, exe, family, version)
                    break
    return sorted(found.values(), key=lambda s: (s.family, s.major_minor), reverse=True)


def candidates_for(family, version, generation, servers):
    """Builds to try for this data, best first. The same major version first: a newer major may
    upgrade the data files on start (fine on the copy, but not always possible), an older one
    usually can't read it at all."""
    want = tuple(int(x) for x in re.findall(r"\d+", version)[:2]) if version else ()
    mysql = [s for s in servers if s.family == "mysql"]
    maria = [s for s in servers if s.family == "mariadb"]
    if family == "mariadb":
        order = maria + [s for s in mysql if s.major_minor < (8, 0)]
    elif generation == "8.x":
        order = [s for s in mysql if s.major_minor >= (8, 0)]
    else:
        same = [s for s in mysql if want and s.major_minor == want]
        others_5 = [s for s in mysql if s.major_minor < (8, 0) and s not in same]
        order = same + others_5 + maria  # MariaDB opens most MySQL 5.x data
    return order
