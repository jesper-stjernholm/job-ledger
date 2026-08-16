"""
SQLite-backed config and state. Replaces config.yaml and state/seen.json.

Schema is deliberately close to SPEC-v2.md section 5, with two additions
tracked in the phase-1 plan: `exclusions.scope` (title vs description, since
v1's rules.description_exclude has no table in the spec) and two settings
columns the spec's list omits but the live config depends on (title_boost,
polite_delay).
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).parent
# STATE_DIR lets hosting decouple the persistent-volume mount point from
# this repo's own layout (e.g. Railway) without changing local/CLI
# behavior at all when unset.
STATE_DIR = Path(os.environ["STATE_DIR"]) if os.environ.get("STATE_DIR") else ROOT / "state"
DB_PATH = STATE_DIR / "job_agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    profile_text        TEXT NOT NULL DEFAULT '',
    site_title          TEXT NOT NULL DEFAULT 'Job Ledger',
    display_threshold   REAL NOT NULL DEFAULT 5.0,
    highlight_threshold REAL NOT NULL DEFAULT 8.0,
    min_affinity        REAL NOT NULL DEFAULT 0.0,
    max_to_model         INTEGER NOT NULL DEFAULT 25,
    description_chars   INTEGER NOT NULL DEFAULT 1200,
    retain_days         INTEGER NOT NULL DEFAULT 21,
    discovery_threshold REAL NOT NULL DEFAULT 7.0,
    batch_size          INTEGER NOT NULL DEFAULT 15,
    title_boost         REAL NOT NULL DEFAULT 3.0,
    polite_delay        REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS roles (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    term    TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exclusions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    term    TEXT NOT NULL,
    scope   TEXT NOT NULL DEFAULT 'title' CHECK (scope IN ('title', 'description')),
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS keywords (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    term    TEXT NOT NULL,
    weight  REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS boards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'builtin' CHECK (kind IN ('builtin', 'custom')),
    adapter      TEXT NOT NULL,
    base_url     TEXT NOT NULL DEFAULT '',
    field_map    TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_ok_at   TEXT,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS searches (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL REFERENCES boards(id),
    label    TEXT NOT NULL DEFAULT '',
    params   TEXT NOT NULL DEFAULT '{}',
    enabled  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS postings (
    uid          TEXT PRIMARY KEY,
    board_id     INTEGER REFERENCES boards(id),
    company      TEXT NOT NULL DEFAULT '',
    company_slug TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    excerpt      TEXT NOT NULL DEFAULT '',
    comp         TEXT NOT NULL DEFAULT '',
    seniority    TEXT NOT NULL DEFAULT '',
    also_on      TEXT NOT NULL DEFAULT '[]',
    affinity     REAL NOT NULL DEFAULT 0,
    score        REAL NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    first_seen   TEXT NOT NULL,
    archived     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    counts       TEXT NOT NULL DEFAULT '{}',
    cost_usd     REAL NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS discovered (
    company     TEXT PRIMARY KEY,
    slug_hint   TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0,
    best_score  REAL NOT NULL DEFAULT 0,
    example_url TEXT NOT NULL DEFAULT '',
    last_seen   TEXT
);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")


# --------------------------------------------------------------------- auth
#
# password_hash and session_secret deliberately do NOT live in job_agent.db.
# That db is committed to a public repo (same reason profile_text is kept
# out of it — see the phase-1 plan). session_secret in particular is a
# signing key: anyone who could read it from git history could forge a
# valid login cookie, regardless of the actual password. So both live in
# their own gitignored file instead.

AUTH_PATH = STATE_DIR / "auth.local.json"


def get_auth() -> dict:
    if AUTH_PATH.exists():
        return json.loads(AUTH_PATH.read_text())
    return {"password_hash": "", "session_secret": ""}


def set_auth(**fields) -> None:
    auth = get_auth()
    auth.update(fields)
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(json.dumps(auth, indent=1))


# --------------------------------------------------------------------- reads

def get_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row) if row else {}


def get_roles(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT term FROM roles WHERE enabled = 1").fetchall()
    return [r["term"] for r in rows]


def get_exclusions(conn: sqlite3.Connection, scope: str) -> list[str]:
    rows = conn.execute(
        "SELECT term FROM exclusions WHERE enabled = 1 AND scope = ?", (scope,)
    ).fetchall()
    return [r["term"] for r in rows]


def get_keywords(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT term, weight FROM keywords WHERE enabled = 1").fetchall()
    return {r["term"]: r["weight"] for r in rows}


def get_boards_and_searches(conn: sqlite3.Connection):
    """
    Returns (companies, searches) shaped exactly like config.yaml's
    `companies:` and `searches:` lists, so sources.collect() needs no change.
    """
    companies, searches = [], []
    boards = conn.execute("SELECT * FROM boards WHERE enabled = 1").fetchall()
    board_by_id = {b["id"]: b for b in boards}

    for b in boards:
        if b["adapter"] in ("greenhouse", "lever", "ashby"):
            companies.append({"name": b["name"], "ats": b["adapter"], "slug": b["base_url"]})

    rows = conn.execute("SELECT * FROM searches WHERE enabled = 1").fetchall()
    for s in rows:
        board = board_by_id.get(s["board_id"])
        if not board:
            continue
        params = json.loads(s["params"])
        entry = {"board": board["adapter"], "label": s["label"], **params}
        searches.append(entry)

    return companies, searches


def get_seen_uids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT uid FROM postings").fetchall()
    return {r["uid"] for r in rows}


def get_board(conn: sqlite3.Connection, retain_days: int, today_iso: str) -> list[dict]:
    from datetime import date, timedelta
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=retain_days)).isoformat()
    rows = conn.execute(
        """SELECT p.*, b.adapter AS source
           FROM postings p LEFT JOIN boards b ON p.board_id = b.id
           WHERE p.archived = 0 AND p.first_seen >= ?
           ORDER BY p.first_seen, p.score""",
        (cutoff,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["also_on"] = json.loads(d["also_on"])
        d["source"] = d["source"] or ""
        d.pop("board_id", None)
        d.pop("archived", None)
        out.append(d)
    return out


# -------------------------------------------------------------------- writes

def _board_id_for(conn: sqlite3.Connection, source: str, uid: str) -> int | None:
    """
    ATS postings match on (adapter, base_url=slug) — the slug is parsed out
    of the uid itself (`greenhouse:{slug}:{id}`) rather than trusted from a
    `company_slug` field, since most ATS fetchers never populate that field
    (only the himalayas fetcher does). Aggregator postings match on adapter
    alone: migration creates exactly one boards row per aggregator adapter.
    """
    if source in ("greenhouse", "lever", "ashby"):
        parts = uid.split(":", 2)
        slug = parts[1] if len(parts) > 1 else ""
        if slug:
            row = conn.execute(
                "SELECT id FROM boards WHERE adapter = ? AND base_url = ?", (source, slug)
            ).fetchone()
            if row:
                return row["id"]
    row = conn.execute("SELECT id FROM boards WHERE adapter = ?", (source,)).fetchone()
    return row["id"] if row else None


def insert_postings(conn: sqlite3.Connection, jobs: list[dict], replace: bool = True) -> None:
    """
    Upsert full posting rows. `replace=True` (default) overwrites — used for
    scored candidates, whose data is authoritative. `replace=False` uses
    INSERT OR IGNORE — used to stamp every raw fetched uid as seen (mirrors
    v1's flat `seen` set) without clobbering a uid that already has real
    score/reason data from a previous run.
    """
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    for j in jobs:
        board_id = _board_id_for(conn, j.get("source", ""), j["uid"])
        conn.execute(
            f"""{verb} INTO postings
               (uid, board_id, company, company_slug, title, location, url,
                excerpt, comp, seniority, also_on, affinity, score, reason,
                first_seen, archived)
               VALUES (:uid, :board_id, :company, :company_slug, :title, :location, :url,
                       :excerpt, :comp, :seniority, :also_on, :affinity, :score, :reason,
                       :first_seen, 0)""",
            {
                "uid": j["uid"], "board_id": board_id,
                "company": j.get("company", ""), "company_slug": j.get("company_slug", ""),
                "title": j.get("title", ""), "location": j.get("location", ""),
                "url": j.get("url", ""), "excerpt": j.get("excerpt", ""),
                "comp": j.get("comp", ""), "seniority": j.get("seniority", ""),
                "also_on": json.dumps(j.get("also_on") or []),
                "affinity": j.get("affinity", 0), "score": j.get("score", 0),
                "reason": j.get("reason", ""), "first_seen": j["first_seen"],
            },
        )


def mark_seen_stubs(conn: sqlite3.Connection, fetched: list[dict], today_iso: str) -> None:
    """Stamp every raw fetched uid as seen, including cross-board merge
    aliases that never survive rank.merge() — matching v1's
    `seen.update(j["uid"] for j in fetched)`, which ran on the pre-merge list."""
    insert_postings(conn, [{**j, "first_seen": today_iso} for j in fetched], replace=False)


def record_run(conn: sqlite3.Connection, started_at: str, finished_at: str,
               counts: dict, cost_usd: float, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, counts, cost_usd, error) VALUES (?, ?, ?, ?, ?)",
        (started_at, finished_at, json.dumps(counts), cost_usd, error),
    )


def upsert_discovered(conn: sqlite3.Connection, company: str, slug_hint: str,
                       best_score: float, example_url: str, title: str, today_iso: str) -> None:
    row = conn.execute("SELECT * FROM discovered WHERE company = ?", (company,)).fetchone()
    if row:
        hits = row["hits"] + 1
        best = max(row["best_score"], best_score)
        example = f"{title} — {example_url}" if best_score > row["best_score"] else row["example_url"]
        conn.execute(
            "UPDATE discovered SET hits = ?, best_score = ?, example_url = ?, last_seen = ? WHERE company = ?",
            (hits, best, example, today_iso, company),
        )
    else:
        conn.execute(
            "INSERT INTO discovered (company, slug_hint, hits, best_score, example_url, last_seen) "
            "VALUES (?, ?, 1, ?, ?, ?)",
            (company, slug_hint, best_score, f"{title} — {example_url}", today_iso),
        )


def list_discovered(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM discovered ORDER BY best_score DESC").fetchall()


def delete_discovered(conn: sqlite3.Connection, company: str) -> None:
    conn.execute("DELETE FROM discovered WHERE company = ?", (company,))


# ------------------------------------------------------------- UI: settings

def update_settings(conn: sqlite3.Connection, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE settings SET {cols} WHERE id = 1", fields)


# --------------------------------------------------------- UI: roles/exclusions/keywords

def list_roles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM roles ORDER BY id").fetchall()


def add_role(conn: sqlite3.Connection, term: str) -> None:
    conn.execute("INSERT INTO roles (term, enabled) VALUES (?, 1)", (term,))


def toggle_role(conn: sqlite3.Connection, role_id: int) -> None:
    conn.execute("UPDATE roles SET enabled = 1 - enabled WHERE id = ?", (role_id,))


def delete_role(conn: sqlite3.Connection, role_id: int) -> None:
    conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))


def list_exclusions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM exclusions ORDER BY scope, id").fetchall()


def add_exclusion(conn: sqlite3.Connection, term: str, scope: str) -> None:
    conn.execute(
        "INSERT INTO exclusions (term, scope, enabled) VALUES (?, ?, 1)", (term, scope)
    )


def toggle_exclusion(conn: sqlite3.Connection, exclusion_id: int) -> None:
    conn.execute("UPDATE exclusions SET enabled = 1 - enabled WHERE id = ?", (exclusion_id,))


def delete_exclusion(conn: sqlite3.Connection, exclusion_id: int) -> None:
    conn.execute("DELETE FROM exclusions WHERE id = ?", (exclusion_id,))


def list_keywords(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM keywords ORDER BY id").fetchall()


def add_keyword(conn: sqlite3.Connection, term: str, weight: float) -> None:
    conn.execute(
        "INSERT INTO keywords (term, weight, enabled) VALUES (?, ?, 1)", (term, weight)
    )


def toggle_keyword(conn: sqlite3.Connection, keyword_id: int) -> None:
    conn.execute("UPDATE keywords SET enabled = 1 - enabled WHERE id = ?", (keyword_id,))


def delete_keyword(conn: sqlite3.Connection, keyword_id: int) -> None:
    conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))


# ------------------------------------------------------------- UI: boards/searches

def list_boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM boards ORDER BY kind, adapter, name").fetchall()


def add_board(conn: sqlite3.Connection, name: str, adapter: str, base_url: str) -> int:
    cur = conn.execute(
        "INSERT INTO boards (name, kind, adapter, base_url, enabled) VALUES (?, 'builtin', ?, ?, 1)",
        (name, adapter, base_url),
    )
    return cur.lastrowid


def toggle_board(conn: sqlite3.Connection, board_id: int) -> None:
    conn.execute("UPDATE boards SET enabled = 1 - enabled WHERE id = ?", (board_id,))


def delete_board(conn: sqlite3.Connection, board_id: int) -> None:
    conn.execute("DELETE FROM searches WHERE board_id = ?", (board_id,))
    conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))


def list_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT s.*, b.name AS board_name, b.adapter AS board_adapter
           FROM searches s JOIN boards b ON s.board_id = b.id
           ORDER BY b.adapter, s.id"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        out.append(d)
    return out


def toggle_search(conn: sqlite3.Connection, search_id: int) -> None:
    conn.execute("UPDATE searches SET enabled = 1 - enabled WHERE id = ?", (search_id,))


# ----------------------------------------------------------------------- UI: runs

def get_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["counts"] = json.loads(d["counts"])
        out.append(d)
    return out
