"""SQLite persistence: page cache (resumability), runs, records with
provenance, cached extractors, and a run-event journal. All writes idempotent."""
import hashlib
import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  url         TEXT PRIMARY KEY,          -- canonical URL
  final_url   TEXT,
  domain      TEXT,
  status      INTEGER,
  method      TEXT,                      -- requests | playwright
  health      TEXT,
  raw_html    BLOB,                      -- zlib-compressed
  content_hash TEXT,
  fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  objective   TEXT,
  start_urls  TEXT,
  fields      TEXT,
  config      TEXT,
  state       TEXT DEFAULT 'running',
  report      TEXT,
  started_at  TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      INTEGER,
  source_url  TEXT,
  extractor   TEXT,                      -- extractor id/version, 'llm', 'structured-data'
  data        TEXT,                      -- JSON, sorted keys
  created_at  TEXT,
  UNIQUE(run_id, source_url, data)
);

CREATE TABLE IF NOT EXISTS extractors (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  domain      TEXT,
  template    TEXT,
  fields      TEXT,                      -- comma-joined, sorted
  spec        TEXT,                      -- JSON
  version     INTEGER DEFAULT 1,
  healthy     INTEGER DEFAULT 1,
  created_at  TEXT,
  UNIQUE(domain, template, fields)
);

CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  ts     TEXT,
  kind   TEXT,
  detail TEXT
);
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- pages ---------------------------------------------------------------

    def get_page(self, url, max_age_days=None):
        row = self.conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        if max_age_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            if fetched < cutoff:
                return None
        return row

    def save_page(self, url, final_url, domain, status, method, health, html):
        raw = zlib.compress(html.encode("utf-8", "replace")) if html else None
        chash = hashlib.sha256(html.encode("utf-8", "replace")).hexdigest()[:16] if html else None
        self.conn.execute(
            """INSERT INTO pages (url, final_url, domain, status, method, health,
                                  raw_html, content_hash, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 final_url=excluded.final_url, domain=excluded.domain,
                 status=excluded.status, method=excluded.method,
                 health=excluded.health, raw_html=excluded.raw_html,
                 content_hash=excluded.content_hash, fetched_at=excluded.fetched_at""",
            (url, final_url, domain, status, method, health, raw, chash, _now()))
        self.conn.commit()

    def page_html(self, url):
        row = self.conn.execute("SELECT raw_html FROM pages WHERE url=?", (url,)).fetchone()
        if row and row["raw_html"]:
            return zlib.decompress(row["raw_html"]).decode("utf-8", "replace")
        return None

    def prune_pages(self, max_age_days):
        """Delete cached pages older than the freshness window. Pages past
        max_age are never served anyway (get_page filters them), so this only
        reclaims space — it never changes behavior. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            "DELETE FROM pages WHERE fetched_at IS NULL OR fetched_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def wipe(self):
        """Delete everything — pages, runs, records, extractors, events.
        Returns {table: rows_deleted}."""
        counts = {}
        for tbl in ("pages", "runs", "records", "extractors", "events"):
            counts[tbl] = self.conn.execute(f"DELETE FROM {tbl}").rowcount
        self.conn.commit()
        return counts

    def vacuum(self):
        self.conn.execute("VACUUM")

    # -- runs / records / events ----------------------------------------------

    def create_run(self, objective, start_urls, fields, config):
        cur = self.conn.execute(
            "INSERT INTO runs (objective, start_urls, fields, config, started_at) "
            "VALUES (?,?,?,?,?)",
            (objective, json.dumps(start_urls), json.dumps(fields),
             json.dumps(config, default=str), _now()))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id, state, report):
        self.conn.execute(
            "UPDATE runs SET state=?, report=?, finished_at=? WHERE id=?",
            (state, json.dumps(report, default=str), _now(), run_id))
        self.conn.commit()

    def add_record(self, run_id, source_url, extractor, data):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO records (run_id, source_url, extractor, data, created_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, source_url, extractor, json.dumps(data, sort_keys=True, default=str),
             _now()))
        self.conn.commit()
        return cur.rowcount  # 0 if duplicate

    def records(self, run_id):
        return self.conn.execute(
            "SELECT * FROM records WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def update_record(self, record_id, data):
        self.conn.execute("UPDATE records SET data=? WHERE id=?",
                          (json.dumps(data, sort_keys=True, default=str), record_id))
        self.conn.commit()

    def log_event(self, run_id, kind, detail):
        self.conn.execute(
            "INSERT INTO events (run_id, ts, kind, detail) VALUES (?,?,?,?)",
            (run_id, _now(), kind, json.dumps(detail, default=str)))
        self.conn.commit()

    # -- extractors -----------------------------------------------------------

    def get_extractor(self, domain, template, fields_key):
        return self.conn.execute(
            "SELECT * FROM extractors WHERE domain=? AND template=? AND fields=? AND healthy=1",
            (domain, template, fields_key)).fetchone()

    def save_extractor(self, domain, template, fields_key, spec, healthy=True):
        self.conn.execute(
            """INSERT INTO extractors (domain, template, fields, spec, healthy, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(domain, template, fields) DO UPDATE SET
                 spec=excluded.spec, healthy=excluded.healthy,
                 version=extractors.version+1, created_at=excluded.created_at""",
            (domain, template, fields_key, json.dumps(spec), int(healthy), _now()))
        self.conn.commit()
        return self.get_extractor(domain, template, fields_key) or self.conn.execute(
            "SELECT * FROM extractors WHERE domain=? AND template=? AND fields=?",
            (domain, template, fields_key)).fetchone()

    def run_row(self, run_id):
        return self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def runs_list(self, limit=20):
        return self.conn.execute(
            "SELECT id, objective, state, started_at, finished_at FROM runs "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
