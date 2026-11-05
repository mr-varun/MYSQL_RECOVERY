# mysql-rescue - what to choose, and when

A practical guide to the recovery window. Go through it top to bottom: pick the data folder, tick
the databases you need, pick one option under **What do you need?**, then **Start rescue**.

The original folder is never changed (it is only read, and checked again at the end), so you can
always run the tool again with other choices.

---

## Quick answer

| Your situation | Choose | Change anything else? |
|---|---|---|
| WAMP / MySQL / MariaDB crashed, the PC lost power, the service won't start any more | **Recover my databases** | No |
| The server starts, but some tables give errors ("Table doesn't exist in engine", "Incorrect information in file", crashes when you open a table) | **Recover my databases** | No |
| The server stops at once with InnoDB errors in its log ("log sequence number is in the future", "corrupted page", assertion failures) | **Recover my databases** first; if every start fails, **Server won't start at all** | No |
| You only have a copy of the `data` folder (from a backup, an old PC, a dead disk) | **Recover my databases** | No |
| You only want to know what is in the folder and what looks wrong | **Just diagnose** | No |

When in doubt: **Recover my databases**.

---

## 1. The damaged data folder

| Field | What to do |
|---|---|
| **The damaged data folder** | Click **Browse...** and pick the `data` folder. Picking the server folder above it (for example WAMP's `bin\mysql\mysql5.6.12`) also works: the tool finds `data\` inside it. The line below then shows the server and version it detected (MySQL 5.6, 5.7, 8.x or MariaDB), the size, and warnings such as "ibdata1 is missing" or "the redo log is probably damaged". |
| **Databases** | One tick box per database, with its number of tables and size, and how many tables look damaged on disk. Untick the ones you don't need: the rescue is faster. System databases (`mysql`, `performance_schema`, `sys`) are never rescued. |
| **Results go to** (**Change...**) | Where results go. Default: the `jobs` folder in the tool folder. Choose a drive with free space of about **3 x the folder size** (a copy of the folder, one working copy, and the dumps). |

The data must match a server build the tool has. It looks in its own `servers\` folder and in the
folders listed in `settings.json` (`server_dirs`). MySQL 5.6, MySQL 8.x and MariaDB data work; for
MySQL 5.7 data without a 5.7 build it tries MariaDB.

---

## 2. What do you need? (choose one)

### Recover my databases  (use this by default)
Starts a matching server on a copy of the folder, **normally first**, and dumps every table one by
one. If the server dies on a table, the tool starts it again on a **fresh copy** with the next
recovery level (`innodb_force_recovery` 1, 2, ... 6) and carries on with the tables that are left.
- Use for: crashes, power cuts, a service that won't start, tables with errors, unknown problems.
- A healthy folder is done after the normal start (level 0): nothing is lost.
- The higher levels are only used for the tables that need them, and the report says which level
  each table needed.

### Server won't start at all
The same, but it skips the normal start and goes straight to recovery levels 1-6.
- Use for: when **Recover my databases** shows that every normal start fails, and you want to save
  the time of that first attempt.

### Just diagnose
Reads the folder only, without starting any server, and explains what it finds: the server and
version, the databases and tables, their storage engines (InnoDB / MyISAM / Aria), sizes, and
problems such as empty or cut-off files, a missing `ibdata1`, or redo log files of different sizes.
- Use for: a first look, or to check a copy before you rescue it.

### What the recovery levels mean

| Level | What the server skips | Risk |
|---|---|---|
| 0 | nothing: a normal start with the usual crash recovery | none |
| 1 | corrupt pages (it reads around them) | rows on a corrupt page can be missing |
| 2 | background threads | none for the data you get |
| 3 | rolling back unfinished transactions | changes of transactions that never committed can be in the data |
| 4 | merging the change buffer | secondary indexes may be out of date (the dumped rows are fine) |
| 5 | the undo logs | rows can be in an older or newer state than the last commit |
| 6 | the redo log | the **last changes before the crash can be missing** |

---

## 3. While it runs

- **Progress** tab: each stage (copy, diagnose, start a server, dump table by table, one .sql per
  database, report) shows ✔ done, ◔ running, ○ waiting. The bar and the line below it show what
  it is working on, for example `bankdb.passbook`.
- **Live log** (dark box): every step, including each server start and the level it used. A line
  that starts with WARN is a warning (for example a table that failed at one level).
- **Stop** ends the run at the next safe point, stops the temporary server, and still writes a report.

---

## 4. After the run - which file to use

The **Result** tab opens by itself: one line per table with its rows, its status (ok / failed) and
the recovery level that worked. Use the files in the job folder (**Open job folder**) like this:

| File | Use it for |
|---|---|
| `<database>.sql` | **The rescued database.** It has no `CREATE DATABASE`, so you can load it into any new name: `mysql -u USER -p NEW_DB < bankdb.sql` (create `NEW_DB` first). Tables, their rows, views, triggers, procedures and events. |
| `report.html` | Read this first: what was found, every server attempt, and per table the rows and the level used. |
| `report.json` | The same, for programs (varsha reads this). |
| `log.txt` | Every step, with times. |
| `diagnosis.json` | What the folder looked like before any server started. |

Before you use a rescued database for real:
1. Keep the original damaged folder.
2. In `report.html`, check the tables that matter: status **ok** and the row counts you expect.
3. If a table needed level **3 or higher**, check its latest rows: see the table above.
4. Load the `.sql` into a **new** database and check it there, before replacing anything.

---

## 5. Jobs (and disk space)

Every run makes a new folder in `jobs\` (or the folder you chose); earlier results are never
overwritten. The **Jobs** tab lists them with their size and how much of that is working files.

| Button | What it does |
|---|---|
| **Show result** (or double-click) | shows that job's result again |
| **Browse** | opens the job folder in Explorer |
| **Clean (keep rescued files)** | removes the working files (`source\`, the copy of your folder, and `tables\`, the per-table dumps) and keeps the `.sql` files, the report and the log. Usually frees most of the space. |
| **Full clean** | deletes the whole job, the `.sql` files too (it asks first). Your original folder is not touched. |

Nothing is deleted unless you choose it.

---

## 6. Recipes

**WAMP's MySQL service won't start after a power cut**
→ Browse to WAMP's `bin\mysql\mysqlX.Y.Z\data` (stop WAMP first) · Recover my databases · Start.
Load `<database>.sql` into a new database and check it.

**One table makes the server crash**
→ Recover my databases. The report shows that table with a higher level and the rest at level 0.

**Only a copy of the data folder is left (old PC, dead disk)**
→ Browse to the copied folder · Recover my databases.

**You want to know if a backup copy of the data folder is usable**
→ Just diagnose. Then Recover my databases if it looks fine.

**Move a database from an old MySQL 5.6 into MariaDB / MySQL 8**
→ Recover my databases on the old folder, then load `<database>.sql` into the new server.

---

## 7. Good to know

- Works with MySQL 5.6 / 5.7 / 8.x and MariaDB data folders, InnoDB and MyISAM / Aria tables.
- The temporary server listens on this PC only (a free port, or a private named pipe for MySQL 8),
  has no passwords, no binary log and no replication, and only ever sees a copy.
- Close the server that normally uses the folder before you rescue it (WAMP, XAMPP, a Windows
  service): a folder that is still being written to gives an inconsistent copy, and the report then
  says the original changed during the run.
- Not yet: repairing broken MyISAM / Aria tables (`myisamchk` / `aria_chk`), rebuilding tables when
  `ibdata1` is lost (from `.frm` / `.ibd`), and bringing back deleted rows (from the binary log or
  the pages). They are planned; until then those tables show as **failed** in the report.
- Command line for every option: `start-cli.cmd` (run it without arguments for help).
