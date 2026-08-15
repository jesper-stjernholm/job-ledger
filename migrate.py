#!/usr/bin/env python3
"""
One-time migration: config.yaml + state/seen.json -> state/job_agent.db.

Run once by hand:

    python migrate.py

Safe to re-run: DELETE state/job_agent.db first if you want a clean rebuild
(this script does not overwrite an existing db).

Deliberately keeps roles/exclusions/keywords as verbatim regex, one row per
current YAML list item — see the phase-1 plan for why (funnel-count parity;
plain-phrase escaping is phase 3 Configuration UI work, not this).
Deliberately leaves settings.profile_text blank — the profile stays sourced
from the PROFILE env var so it never ends up in the committed db file on a
public repo.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

import db

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "seen.json"


def migrate_settings(conn, cfg: dict) -> None:
    affinity = cfg.get("affinity", {})
    conn.execute(
        """UPDATE settings SET
            site_title = ?, display_threshold = ?, highlight_threshold = ?,
            min_affinity = ?, max_to_model = ?, description_chars = ?,
            retain_days = ?, discovery_threshold = ?, batch_size = ?,
            title_boost = ?, polite_delay = ?
           WHERE id = 1""",
        (
            cfg.get("site_title", "Job Ledger"),
            cfg.get("display_threshold", 5.0),
            cfg.get("highlight_threshold", 8.0),
            affinity.get("min_score", 0.0),
            # the effective cap today is affinity.max_to_model (applied first,
            # inside rank.py); max_scored_per_run never binds at current
            # values (40 > 25) — spec collapses these into one, so migrate
            # the one that actually does something.
            affinity.get("max_to_model", 25),
            cfg.get("description_chars", 1200),
            cfg.get("retain_days", 21),
            cfg.get("discovery_threshold", 7.0),
            cfg.get("batch_size", 15),
            affinity.get("title_boost", 3.0),
            cfg.get("polite_delay", 1.0),
        ),
    )


def migrate_rules(conn, cfg: dict) -> tuple[int, int, int]:
    rules = cfg.get("rules", {})
    roles = rules.get("title_include") or []
    for term in roles:
        conn.execute("INSERT INTO roles (term, enabled) VALUES (?, 1)", (term,))

    title_excl = rules.get("title_exclude") or []
    for term in title_excl:
        conn.execute(
            "INSERT INTO exclusions (term, scope, enabled) VALUES (?, 'title', 1)", (term,)
        )

    desc_excl = rules.get("description_exclude") or []
    for term in desc_excl:
        conn.execute(
            "INSERT INTO exclusions (term, scope, enabled) VALUES (?, 'description', 1)", (term,)
        )

    return len(roles), len(title_excl), len(desc_excl)


def migrate_keywords(conn, cfg: dict) -> int:
    kw = cfg.get("affinity", {}).get("keywords", {}) or {}
    for term, weight in kw.items():
        conn.execute(
            "INSERT INTO keywords (term, weight, enabled) VALUES (?, ?, 1)", (term, float(weight))
        )
    return len(kw)


# Adapter -> the endpoint sources.py actually calls, for documentation on the
# boards row. Not used at fetch time (sources.py hardcodes these); just makes
# `boards.base_url` legible if someone inspects the db.
AGGREGATOR_URLS = {
    "himalayas": "https://himalayas.app/jobs/api/search",
    "remotive": "https://remotive.com/api/remote-jobs",
    "remoteok": "https://remoteok.com/api",
}


def migrate_boards_and_searches(conn, cfg: dict) -> tuple[int, int]:
    board_count = 0

    companies = cfg.get("companies") or []
    for c in companies:
        conn.execute(
            "INSERT INTO boards (name, kind, adapter, base_url, enabled) VALUES (?, 'builtin', ?, ?, 1)",
            (c.get("name", c["slug"]), c["ats"], c["slug"]),
        )
        board_count += 1

    searches = cfg.get("searches") or []
    adapter_board_id: dict[str, int] = {}
    search_count = 0
    for s in searches:
        adapter = s["board"]
        if adapter not in adapter_board_id:
            cur = conn.execute(
                "INSERT INTO boards (name, kind, adapter, base_url, enabled) VALUES (?, 'builtin', ?, ?, 1)",
                (adapter.capitalize(), adapter, AGGREGATOR_URLS.get(adapter, "")),
            )
            adapter_board_id[adapter] = cur.lastrowid
            board_count += 1

        params = {k: v for k, v in s.items() if k not in ("board", "label")}
        conn.execute(
            "INSERT INTO searches (board_id, label, params, enabled) VALUES (?, ?, ?, 1)",
            (adapter_board_id[adapter], s.get("label", ""), json.dumps(params)),
        )
        search_count += 1

    return board_count, search_count


def migrate_postings_and_discovered(conn, state: dict) -> tuple[int, int]:
    board = state.get("board", [])
    for j in board:
        db.insert_postings(conn, [j], replace=True)

    discovered = state.get("discovered", {})
    for company, meta in discovered.items():
        conn.execute(
            """INSERT INTO discovered (company, slug_hint, hits, best_score, example_url, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                company, meta.get("slug_hint", ""), meta.get("hits", 0),
                meta.get("best_score", 0), meta.get("example", ""), meta.get("last_seen"),
            ),
        )

    return len(board), len(discovered)


def main() -> int:
    if db.DB_PATH.exists():
        print(f"! {db.DB_PATH} already exists. Delete it first if you want a clean rebuild.",
              file=sys.stderr)
        return 1

    if not CONFIG_PATH.exists():
        print(f"! {CONFIG_PATH} not found.", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

    with db.connect() as conn:
        db.init_schema(conn)
        migrate_settings(conn, cfg)
        n_roles, n_title_excl, n_desc_excl = migrate_rules(conn, cfg)
        n_kw = migrate_keywords(conn, cfg)
        n_boards, n_searches = migrate_boards_and_searches(conn, cfg)
        n_postings, n_discovered = migrate_postings_and_discovered(conn, state)

        raw_seen = set(state.get("seen", []))
        board_uids = {j["uid"] for j in state.get("board", [])}
        missing = raw_seen - board_uids
        if missing:
            # These are postings that were fetched and rejected before ever
            # reaching the model (failed rules, or ranked below the cut) —
            # v1's flat seen list has them, but state["board"] never did.
            # They don't need migrating: they'll be re-fetched on the next
            # run, re-rejected by the same unchanged rules, and re-marked
            # seen for free. Cost impact: zero (they never reached scoring).
            print(f"  note: {len(missing)} previously-seen uids were never scored "
                  f"(rejected by rules/ranking) and aren't carried into postings. "
                  f"They'll be silently re-marked seen on the next run.")

    print("\nmigrated into", db.DB_PATH)
    print(f"  settings          1 row")
    print(f"  roles             {n_roles}")
    print(f"  exclusions        {n_title_excl + n_desc_excl}  ({n_title_excl} title, {n_desc_excl} description)")
    print(f"  keywords          {n_kw}")
    print(f"  boards            {n_boards}")
    print(f"  searches          {n_searches}")
    print(f"  postings          {n_postings}")
    print(f"  discovered        {n_discovered}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
