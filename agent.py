#!/usr/bin/env python3
"""
Daily job agent.

Pipeline: fetch -> dedup -> rule filter -> LLM score -> render HTML.

Only the scoring step calls a model, and it only ever sees postings that are
new since the last run and survived the local rules. Everything else is
deterministic code.

Usage:
    python agent.py            # full run
    python agent.py --dry-run  # fetch + filter only, no API call, no state write
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timezone
from pathlib import Path

import db
import rank
import sources
from sources import strip_html

ROOT = Path(__file__).parent
OUTPUT_PATH = ROOT / "docs" / "index.html"

MODEL = "claude-haiku-4-5-20251001"
PRICE_IN = 1.00 / 1_000_000   # USD per input token
PRICE_OUT = 5.00 / 1_000_000  # USD per output token

TIMEOUT = 30
UA = {"User-Agent": "personal-job-agent/1.0"}


# ---------------------------------------------------------------- data model

@dataclass
class Job:
    uid: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    comp: str = ""
    source: str = ""
    seniority: str = ""
    company_slug: str = ""
    also_on: list = field(default_factory=list)
    affinity: float = 0.0
    score: float = 0.0
    reason: str = ""
    first_seen: str = ""


# ------------------------------------------------------------- rule filtering

def compile_patterns(patterns: list[str] | None):
    return [re.compile(p, re.I) for p in (patterns or [])]


def rule_filter(jobs: list[dict], rules: dict) -> list[dict]:
    include = compile_patterns(rules.get("title_include"))
    exclude = compile_patterns(rules.get("title_exclude"))
    locations = compile_patterns(rules.get("location_include"))
    body_exclude = compile_patterns(rules.get("description_exclude"))

    kept = []
    for job in jobs:
        title, loc = job.get("title", ""), job.get("location", "")
        body = job.get("description") or job.get("excerpt", "")
        if include and not any(p.search(title) for p in include):
            continue
        if any(p.search(title) for p in exclude):
            continue
        if locations and not any(p.search(loc) for p in locations):
            continue
        if any(p.search(body) for p in body_exclude):
            continue
        kept.append(job)
    return kept


# -------------------------------------------------------------------- scoring

SYSTEM = (
    "You screen job postings for one candidate. You are strict and terse. "
    "You reply with JSON only, never prose, never code fences."
)

TEMPLATE = """<candidate_profile>
{profile}
</candidate_profile>

Score each posting below from 0 to 10 on fit with this candidate.
Calibration: 8+ means apply today; 5-7 means plausible but imperfect;
below 5 means skip. Weigh seniority match, required skills, and
location or work-authorisation constraints heavily. Most postings are not
a good fit — do not inflate scores.

Reply with a JSON array only, one object per posting, in the same order:
[{{"n": 1, "score": 7.5, "reason": "max 18 words, concrete"}}]

<postings>
{postings}
</postings>"""


def build_batch(jobs: list[Job], desc_chars: int) -> str:
    blocks = []
    for i, job in enumerate(jobs, 1):
        desc = job.description[:desc_chars]
        blocks.append(
            f"[{i}] {job.title} — {job.company} — {job.location}\n{desc}"
        )
    return "\n\n".join(blocks)


def parse_scores(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def score_jobs(jobs: list[Job], profile: str, cfg: dict,
                max_cost: float | None = None) -> tuple[list[Job], float]:
    """
    One API call per batch. Returns scored jobs and actual USD cost.

    `max_cost`, if set, is checked before each batch against cost actually
    spent so far (from `resp.usage` on prior batches, not an estimate) —
    if the next batch would start after the cap is already reached, it and
    every remaining batch are skipped. Jobs in skipped batches keep their
    default score (0), so they simply won't clear display_threshold rather
    than silently vanishing.
    """
    from anthropic import Anthropic

    client = Anthropic()
    batch_size = cfg.get("batch_size", 15)
    desc_chars = cfg.get("description_chars", 1200)
    cost = 0.0

    for start in range(0, len(jobs), batch_size):
        if max_cost is not None and cost >= max_cost:
            skipped = len(jobs) - start
            print(f"  ! stopping: reached max cost cap (${cost:.4f} >= ${max_cost:.2f}); "
                  f"{skipped} posting(s) left unscored", file=sys.stderr)
            break
        chunk = jobs[start:start + batch_size]
        prompt = TEMPLATE.format(profile=profile, postings=build_batch(chunk, desc_chars))
        resp = client.messages.create(
            model=MODEL,
            max_tokens=100 * len(chunk) + 200,
            temperature=0,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        cost += resp.usage.input_tokens * PRICE_IN + resp.usage.output_tokens * PRICE_OUT

        try:
            results = parse_scores(resp.content[0].text)
        except (json.JSONDecodeError, IndexError) as exc:
            print(f"  ! unparseable batch at {start}: {exc}", file=sys.stderr)
            continue

        by_index = {int(r["n"]): r for r in results if "n" in r}
        for i, job in enumerate(chunk, 1):
            result = by_index.get(i)
            if result:
                job.score = float(result.get("score", 0))
                job.reason = str(result.get("reason", ""))[:160]

    return jobs, cost


# --------------------------------------------------------------------- render

CSS = """
:root{
  --ground:#e9edf1; --card:#fdfeff; --ink:#121b2a; --soft:#5a6879;
  --rule:#c9d2dc; --strong:#0b5f52; --fresh:#a8560b;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--ground); color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,sans-serif;
  font-size:16px; line-height:1.5;
  padding:clamp(20px,5vw,64px) clamp(16px,5vw,32px);
}
.wrap{max-width:820px;margin:0 auto}

header{border-bottom:2px solid var(--ink);padding-bottom:14px;margin-bottom:8px}
h1{
  font-family:"Instrument Serif",Georgia,serif;
  font-size:clamp(38px,8vw,60px); font-weight:400; line-height:.95;
  letter-spacing:-.01em;
}
.meta{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--soft); margin-top:10px;
  display:flex; flex-wrap:wrap; gap:6px 18px;
}

.nav{
  display:flex; gap:18px; padding:14px 0 0;
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11px; letter-spacing:.09em; text-transform:uppercase;
}
.nav a{color:var(--soft); text-decoration:none; padding-bottom:2px; border-bottom:2px solid transparent}
.nav a:hover,.nav a[aria-current="page"]{color:var(--ink); border-color:var(--strong)}

.day{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11px; letter-spacing:.14em; text-transform:uppercase;
  color:var(--soft); padding:26px 0 8px;
  display:flex; justify-content:space-between; align-items:baseline;
}

.row{
  display:grid; grid-template-columns:62px 1fr; gap:0 18px;
  background:var(--card); border:1px solid var(--rule);
  border-radius:2px; padding:14px 18px 14px 0; margin-bottom:6px;
  transition:border-color .12s ease;
}
.row:hover,.row:focus-within{border-color:var(--ink)}

.gutter{
  border-right:1px solid var(--rule); padding:2px 14px 2px 18px;
  text-align:right;
}
.score{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:21px; font-weight:500; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; color:var(--soft);
}
.row[data-tier="high"] .score{color:var(--strong)}
.row[data-tier="high"]{border-left:2px solid var(--strong)}
.flag{
  display:block; margin-top:3px; color:var(--fresh);
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:9px; letter-spacing:.12em; text-transform:uppercase;
}

.title{font-size:17px; font-weight:600; line-height:1.3}
.title a{color:inherit; text-decoration:none}
.title a:hover{text-decoration:underline; text-underline-offset:3px}
.title a:focus-visible{outline:2px solid var(--strong); outline-offset:3px}
.where{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11.5px; color:var(--soft); margin-top:4px;
}
.why{font-size:14px; color:var(--soft); margin-top:7px; max-width:56ch}
.srcs{margin-top:8px; display:flex; flex-wrap:wrap; gap:5px}
.src{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:9px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--soft); border:1px solid var(--rule);
  border-radius:2px; padding:2px 6px;
}
footer a{color:var(--soft)}

.empty{
  border:1px dashed var(--rule); border-radius:2px;
  padding:40px 24px; text-align:center; color:var(--soft); font-size:15px;
}
footer{
  margin-top:44px; padding-top:14px; border-top:1px solid var(--rule);
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:10.5px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--soft);
}
@media (max-width:520px){
  .row{grid-template-columns:52px 1fr; gap:0 12px; padding-right:14px}
  .gutter{padding-left:12px; padding-right:10px}
  .score{font-size:18px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;600&family=Instrument+Serif&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{title}</h1>
  <div class="meta">
    <span>Updated {updated}</span>
    <span>{total} open &middot; {new} new today</span>
    <span>{watched} boards watched</span>
  </div>
</header>
{nav}
{body}
<footer>Scored by {model} &middot; {cost} this run &middot; postings expire after {ttl} days{credits}</footer>
</div>
</body>
</html>"""


def esc(text: str) -> str:
    return htmllib.escape(text or "", quote=True)


def render(board: list[Job], cfg: dict, cost: float, watched: int,
           used_boards: set | None = None, nav_html: str = "") -> str:
    today = date.today().isoformat()
    threshold = cfg.get("display_threshold", 5.0)
    shown = [j for j in board if j.score >= threshold]
    shown.sort(key=lambda j: (j.first_seen, j.score), reverse=True)

    if not shown:
        body = ('<div class="empty">No postings above the score threshold yet. '
                'The agent runs again tomorrow morning.</div>')
    else:
        chunks, current = [], None
        for job in shown:
            if job.first_seen != current:
                current = job.first_seen
                stamp = datetime.fromisoformat(current).strftime("%a %d %b").upper()
                count = sum(1 for j in shown if j.first_seen == current)
                label = "arrival" if count == 1 else "arrivals"
                chunks.append(
                    f'<div class="day"><span>{stamp}</span>'
                    f'<span>{count} {label}</span></div>'
                )
            tier = "high" if job.score >= cfg.get("highlight_threshold", 8.0) else "std"
            flag = '<span class="flag">New</span>' if job.first_seen == today else ""
            where = " &middot; ".join(filter(None, [
                esc(job.company), esc(job.location or "Location not listed"), esc(job.comp)
            ]))
            why = f'<p class="why">{esc(job.reason)}</p>' if job.reason else ""
            badges = " ".join(
                f'<span class="src">{esc(s)}</span>'
                for s in [job.source, *(job.also_on or [])])
            chunks.append(
                f'<article class="row" data-tier="{tier}">'
                f'<div class="gutter"><span class="score">{job.score:.1f}</span>{flag}</div>'
                f'<div><h2 class="title"><a href="{esc(job.url)}" target="_blank" '
                f'rel="noopener">{esc(job.title)}</a></h2>'
                f'<p class="where">{where}</p>{why}'
                f'<p class="srcs">{badges}</p></div></article>'
            )
        body = "\n".join(chunks)

    credits = ""
    if used_boards:
        links = [f'<a href="{u}">{n}</a>' for key, (n, u) in sources.ATTRIBUTION.items()
                 if key in used_boards]
        if links:
            credits = " &middot; listings via " + ", ".join(links)

    return PAGE.format(
        credits=credits,
        title=esc(cfg.get("site_title", "Job Ledger")),
        css=CSS,
        updated=datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        total=len(shown),
        new=sum(1 for j in shown if j.first_seen == today),
        watched=watched,
        model=MODEL,
        cost=f"${cost:.4f}" if cost else "no API calls",
        ttl=cfg.get("retain_days", 21),
        nav=nav_html,
        body=body,
    )


# ----------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch, dedup and rank only; no API call, no state written")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="stop scoring once actual spend this run reaches this many USD")
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()

    with db.connect() as conn:
        db.init_schema(conn)
        settings = db.get_settings(conn)
        profile = os.environ.get("PROFILE") or settings.get("profile_text", "")
        if not profile.strip() and not args.dry_run:
            print("No profile set. Put it in the PROFILE env var.", file=sys.stderr)
            return 1

        rules = {
            "title_include": db.get_roles(conn),
            "title_exclude": db.get_exclusions(conn, "title"),
            "location_include": db.get_locations(conn),
            "description_exclude": db.get_exclusions(conn, "description"),
        }
        affinity_cfg = {
            "keywords": db.get_keywords(conn),
            "title_boost": settings["title_boost"],
            "min_score": settings["min_affinity"],
            "max_to_model": settings["max_to_model"],
        }

        companies, searches = db.get_boards_and_searches(conn)
        seen = db.get_seen_uids(conn)

        fetched, used_boards = sources.collect(companies, searches, settings["polite_delay"])
        print(f"\n{len(fetched):>5} fetched")

        fresh = [j for j in fetched if j["uid"] not in seen]
        print(f"{len(fresh):>5} not seen before")

        # Cross-source dedup BEFORE rules and ranking: the same role on three
        # boards is one decision, not three.
        deduped = rank.merge(fresh)
        if len(fresh) != len(deduped):
            print(f"{len(deduped):>5} after cross-board dedup ({len(fresh) - len(deduped)} duplicates)")

        passed = rule_filter(deduped, rules)
        print(f"{len(passed):>5} passed local rules")

        shortlist, overflow = rank.rank(passed, {"affinity": affinity_cfg})
        if overflow:
            print(f"{len(overflow):>5} ranked below the cut (cheapest rejection there is)")
        print(f"{len(shortlist):>5} going to the model")

        if args.dry_run:
            print("\n--dry-run: no API call. Top of the shortlist:\n")
            for job in shortlist[:15]:
                marks = f" +{','.join(job['also_on'])}" if job.get("also_on") else ""
                print(f"  {job['affinity']:>6.1f}  {job['company'][:18]:<18} "
                      f"{job['title'][:44]:<44} [{job['source']}{marks}]")
            if overflow:
                print("\n  just below the cut:")
                for job in overflow[:5]:
                    print(f"  {job['affinity']:>6.1f}  {job['company'][:18]:<18} {job['title'][:44]}")
            return 0

        candidates = [Job(**{k: v for k, v in j.items() if k in Job.__annotations__})
                      for j in shortlist]

        cost = 0.0
        if candidates:
            cost_note = f" (capped at ${args.max_cost:.2f})" if args.max_cost is not None else ""
            print(f"\nscoring {len(candidates)} in batches of {settings['batch_size']}{cost_note}...")
            candidates, cost = score_jobs(candidates, profile, settings, max_cost=args.max_cost)
            print(f"actual cost: ${cost:.4f}")

        today = date.today().isoformat()
        for job in candidates:
            job.first_seen = today

        # ---- company discovery loop --------------------------------------
        # This is what aggregators are actually for. Any aggregator posting
        # that scores well means the COMPANY is worth watching directly —
        # their ATS board is authoritative, immediate, and has no 24h
        # aggregator lag. Check the `discovered` table weekly and promote
        # the good ones into `boards`.
        threshold = settings["discovery_threshold"]
        known = {c["slug"].lower() for c in companies}
        known |= {c.get("name", "").lower() for c in companies}
        for job in candidates:
            if job.source in sources.ATS_FETCHERS or job.score < threshold:
                continue
            if job.company.lower() in known:
                continue
            db.upsert_discovered(conn, job.company, job.company_slug,
                                  job.score, job.url, job.title, today)

        top = conn.execute(
            "SELECT company, hits, best_score FROM discovered ORDER BY best_score DESC LIMIT 5"
        ).fetchall()
        if top:
            print("\ncompanies worth adding as a board (see `discovered` table):")
            for row in top:
                print(f"  {row['best_score']:>4.1f}  {row['company']}  ({row['hits']} good posting(s))")

        # Authoritative write for scored candidates first, then stamp every
        # raw fetched uid (including cross-board merge aliases) as seen —
        # IGNORE won't clobber the real data just written.
        db.insert_postings(conn, [asdict(j) for j in candidates], replace=True)
        db.mark_seen_stubs(conn, fetched, today)

        retain_days = settings["retain_days"]
        board_rows = db.get_board(conn, retain_days, today)
        board = [Job(**{k: v for k, v in r.items() if k in Job.__annotations__})
                 for r in board_rows]

        finished_at = datetime.now(timezone.utc).isoformat()
        counts = {"fetched": len(fetched), "new": len(fresh), "deduped": len(deduped),
                  "passed_rules": len(passed), "ranked": len(shortlist), "scored": len(candidates)}
        db.record_run(conn, started_at, finished_at, counts, cost)

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(render(board, settings, cost, len(companies), used_boards))
        print(f"\nwrote {OUTPUT_PATH} ({len(board)} on board)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
