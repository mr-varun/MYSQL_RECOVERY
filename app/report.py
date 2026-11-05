"""report.json (for programs, e.g. varsha) and report.html (for people) of one rescue job."""
import html
from pathlib import Path


def build(job, diag, results, outputs, attempts):
    databases = []
    for db, tables in results.items():
        rows = sorted(tables.values(), key=lambda t: t["table"].lower())
        databases.append({
            "database": db, "tables": rows,
            "ok": sum(1 for t in rows if t["status"] == "ok"),
            "failed": sum(1 for t in rows if t["status"] == "failed"),
            "rows": sum(t.get("rows") or 0 for t in rows if t["status"] == "ok"),
        })
    all_tables = [t for d in databases for t in d["tables"]]
    totals = {"databases": len(databases), "tables": len(all_tables),
              "tables_ok": sum(1 for t in all_tables if t["status"] == "ok"),
              "tables_failed": sum(1 for t in all_tables if t["status"] == "failed"),
              "rows": sum(t.get("rows") or 0 for t in all_tables if t["status"] == "ok"),
              "highest_level": max([t.get("level") or 0 for t in all_tables if t["status"] == "ok"] or [0])}
    totals["percent"] = round(100.0 * totals["tables_ok"] / totals["tables"], 1) if totals["tables"] else 0.0
    return {"source": job.results["source"], "job_dir": str(job.dir), "started": job.results["started"],
            "size": job.results.get("size"), "files": job.results.get("files"),
            "original_unchanged": job.results.get("original_unchanged"),
            "diagnosis": {k: v for k, v in (diag or {}).items() if k != "databases"},
            "found": [{"database": d["name"], "tables": len(d["tables"]), "size": d["size"], "problems": d["problems"],
                       "system": d["system"]} for d in (diag or {}).get("databases", [])],
            "attempts": attempts, "databases": databases, "totals": totals, "outputs": outputs}


def write_html(report, path):
    e = lambda v: html.escape(str(v if v is not None else ""))  # noqa: E731
    t = report["totals"]
    d = report.get("diagnosis") or {}
    out = ["<!doctype html><meta charset='utf-8'><title>mysql-rescue report</title><style>",
           "body{font:14px/1.5 Segoe UI,Arial,sans-serif;margin:24px;color:#1b1f27;max-width:1100px}",
           "table{border-collapse:collapse;margin:8px 0 18px}td,th{border:1px solid #d5d9e0;padding:4px 10px;text-align:left}",
           "th{background:#f2f4f7}.ok{color:#11804a;font-weight:600}.failed{color:#c0362c;font-weight:600}",
           ".num{text-align:right;font-family:Consolas,monospace}code{background:#f2f4f7;padding:0 4px}</style>",
           "<h1>mysql-rescue report</h1>",
           "<p><b>Data folder:</b> <code>%s</code><br><b>Detected:</b> %s %s (%s)<br><b>Started:</b> %s</p>"
           % (e(report["source"]), e(d.get("family")), e(d.get("version")), e(d.get("generation")), e(report["started"])),
           "<h2>Result</h2><p>%s of %s tables recovered (%s%%), %s rows. Highest recovery level used: %s.</p>"
           % (e(t["tables_ok"]), e(t["tables"]), e(t["percent"]), e(f"{t['rows']:,}"), e(t["highest_level"])),
           "<p>The original folder was %s.</p>" % ("checked again at the end and is unchanged"
                                                   if report.get("original_unchanged") else "<b>not confirmed unchanged</b>")]
    if d.get("warnings"):
        out.append("<h2>Warnings</h2><ul>%s</ul>" % "".join("<li>%s</li>" % e(w) for w in d["warnings"]))
    out.append("<h2>Server attempts</h2><table><tr><th>Server</th><th>Recovery level</th><th>Result</th></tr>")
    for a in report["attempts"]:
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (e(a["server"]), e(a["level"]), e(a["result"])))
    out.append("</table>")
    for db in report["databases"]:
        out.append("<h2>%s</h2><p>%s tables recovered, %s failed, %s rows.</p>" % (e(db["database"]), e(db["ok"]),
                                                                                  e(db["failed"]), e(f"{db['rows']:,}")))
        out.append("<table><tr><th>Table</th><th>Engine</th><th>Rows</th><th>Status</th><th>Level</th><th>Note</th></tr>")
        for row in db["tables"]:
            out.append("<tr><td>%s</td><td>%s</td><td class='num'>%s</td><td class='%s'>%s</td><td class='num'>%s</td><td>%s</td></tr>"
                       % (e(row["table"]), e(row.get("engine")), e("" if row.get("rows") is None else f"{row['rows']:,}"),
                          e(row["status"]), e(row["status"]), e("" if row.get("level") is None else row["level"]),
                          e(row.get("error") or "")))
        out.append("</table>")
    if report.get("outputs"):
        out.append("<h2>Files</h2><ul>%s</ul>" % "".join("<li>%s: <code>%s</code></li>" % (e(k), e(v))
                                                       for k, v in report["outputs"].items()))
    Path(path).write_text("\n".join(out), encoding="utf-8")
