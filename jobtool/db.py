"""Local SQLite store of every job the tool has seen, so nothing comes back twice."""
import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    alt_keys TEXT DEFAULT '',
    company TEXT, title TEXT, location TEXT, url TEXT, ats TEXT,
    posted TEXT, first_seen TEXT,
    status TEXT DEFAULT 'new',   -- new, filled, submitted, skipped, needs_you, closed
    resume TEXT, notes TEXT DEFAULT '', updated TEXT
);
"""

STATUSES = ("new", "filled", "submitted", "skipped", "needs_you", "closed")


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(jobs)")}
        for col in ("submitted_at", "exported_at", "recruiter"):   # added later: upgrade older databases
            if col not in cols:
                self.con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
        self.con.commit()

    def seen_keys(self):
        keys = set()
        for r in self.con.execute("SELECT key, alt_keys FROM jobs"):
            keys.add(r["key"])
            keys.update(k for k in (r["alt_keys"] or "").split(",") if k)
        return keys

    def known_url(self, url):
        return self.con.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone() is not None

    def same_role_exists(self, company, title):
        return self.con.execute(
            "SELECT id, status FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?)",
            (company, title)).fetchone()

    def reopen(self, job_id, j):
        """A closed job was reposted: point the existing row at the new listing and put it back in the queue."""
        row = self.con.execute("SELECT alt_keys, notes FROM jobs WHERE id = ?", (job_id,)).fetchone()
        alt = ",".join(x for x in [row["alt_keys"], j["key"], *j.get("alt_keys", [])] if x)
        note = " | ".join(x for x in ["REPOSTED", j.get("notes")] if x)
        self.set(job_id, status="new", url=j.get("url"), ats=j.get("ats"), posted=j.get("posted"),
                 location=j.get("location"), alt_keys=alt, notes=note)

    def add(self, j, resume=None):
        today = date.today().isoformat()
        try:
            self.con.execute(
                "INSERT INTO jobs (key, alt_keys, company, title, location, url, ats, posted, first_seen,"
                " resume, updated, notes, recruiter) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (j["key"], ",".join(j.get("alt_keys", [])), j.get("company"), j.get("title"),
                 j.get("location"), j.get("url"), j.get("ats"), j.get("posted"), today, resume, today,
                 j.get("notes") or "", j.get("recruiter")))
            self.con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def jobs(self, status=None, limit=None, ids=None):
        q, args = "SELECT * FROM jobs", []
        if ids:
            q += " WHERE id IN (%s)" % ",".join("?" * len(ids))
            args += list(ids)
        elif status:
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY COALESCE(posted, first_seen) DESC, id DESC"
        if limit:
            q += " LIMIT ?"
            args.append(limit)
        return self.con.execute(q, args).fetchall()

    def set(self, job_id, **fields):
        fields["updated"] = date.today().isoformat()
        if fields.get("status") == "submitted" and "submitted_at" not in fields:
            fields["submitted_at"] = fields["updated"]
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.con.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
        self.con.commit()

    def forget(self, since, keep_submitted=True):
        q = "DELETE FROM jobs WHERE first_seen >= ?"
        if keep_submitted:
            q += " AND status != 'submitted'"
        n = self.con.execute(q, (since,)).rowcount
        self.con.commit()
        return n

    def applied(self, include_exported=False, since=None):
        q = "SELECT * FROM jobs WHERE status = 'submitted'"
        args = []
        if not include_exported:
            q += " AND exported_at IS NULL"
        if since:
            q += " AND COALESCE(submitted_at, updated) >= ?"
            args.append(since)
        q += " ORDER BY COALESCE(submitted_at, updated), id"
        return self.con.execute(q, args).fetchall()

    def mark_exported(self, ids):
        today = date.today().isoformat()
        self.con.executemany("UPDATE jobs SET exported_at = ? WHERE id = ?", [(today, i) for i in ids])
        self.con.commit()
