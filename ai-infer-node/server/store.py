import json
import sqlite3
import time


ACTIVE = ("loading", "running", "saving", "unloading")
TERMINAL = ("completed", "failed", "canceled")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, fingerprint TEXT NOT NULL, options TEXT NOT NULL,
            extension TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            progress REAL DEFAULT 0, detail TEXT DEFAULT '', error TEXT DEFAULT '', metrics TEXT DEFAULT '{}',
            canceled INTEGER DEFAULT 0, pid INTEGER, start_ticks TEXT
        )""")
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        if 'detail' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN detail TEXT DEFAULT ''")
        if 'result_expired' not in columns:
            self.db.execute('ALTER TABLE jobs ADD COLUMN result_expired INTEGER DEFAULT 0')
        if 'execution_snapshot' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN execution_snapshot TEXT DEFAULT '{}'")
        self.db.commit()

    def get(self, ident):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None

    def by_key(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE idem=?", (key,)).fetchone()
        return dict(row) if row else None

    def add(self, ident, key, kind, fingerprint, options, extension, execution_snapshot=None):
        now = time.time()
        self.db.execute("INSERT INTO jobs(id,idem,kind,status,fingerprint,options,extension,created,updated) VALUES(?,?,?,'queued',?,?,?,?,?)",
                        (ident, key, kind, fingerprint, json.dumps(options, sort_keys=True), extension, now, now))
        self.db.execute('UPDATE jobs SET execution_snapshot=? WHERE id=?', (json.dumps(execution_snapshot or {}, sort_keys=True), ident))
        self.db.commit()

    def update(self, ident, **values):
        allowed = {"status", "progress", "detail", "error", "metrics", "canceled", "pid", "start_ticks"}
        assert set(values) <= allowed
        values["updated"] = time.time()
        fields = ",".join(f"{key}=?" for key in values)
        self.db.execute(f"UPDATE jobs SET {fields} WHERE id=?", (*values.values(), ident))
        self.db.commit()

    def queued(self):
        row = self.db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        return row[0] if row else None

    def counts(self):
        return dict(self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())

    def unfinished(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs WHERE status IN ('loading','running','saving','unloading')")]

    def expired(self, before):
        return [row[0] for row in self.db.execute("SELECT id FROM jobs WHERE updated<? AND result_expired=0 AND status IN ('completed','failed','canceled')", (before,))]

    def remove(self, ident):
        self.db.execute('UPDATE jobs SET result_expired=1 WHERE id=?', (ident,))
        self.db.commit()


def public_job(job):
    snapshot = json.loads(job.get('execution_snapshot') or '{}')
    return {key: job[key] for key in ("id", "kind", "status", "progress", "detail", "error", "created", "updated")} | {"metrics": json.loads(job["metrics"]), "result_expired": bool(job.get("result_expired", 0)), "acceptance": "accepted", 'operation_key': job['idem'], 'input_sha256': snapshot.get('input_sha256'), 'options': json.loads(job['options'])}
