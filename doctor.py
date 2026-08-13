#!/usr/bin/env python3
"""
Why did I get no jobs?

Runs the pipeline stage by stage and reports where the numbers hit zero.
Makes no API calls and writes no state, so it's free and safe to re-run.

    python doctor.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

import rank
import sources
from agent import compile_patterns

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.yaml"
STATE = ROOT / "state" / "seen.json"

OK, BAD, WARN = "  ok ", "FAIL ", "WARN "


def hr(title):
    print(f"\n{'─' * 66}\n{title}\n{'─' * 66}")


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text())
    verdicts = []

    # ---------------------------------------------------------------- config
    hr("1. CONFIG")
    companies = cfg.get("companies", [])
    searches = cfg.get("searches", [])
    print(f"{OK}{len(companies)} ATS companies, {len(searches)} aggregator searches")

    profile = cfg.get("profile", "")
    if "Backend engineer, 8 years" in profile:
        print(f"{WARN}profile is still the example text I shipped")
        verdicts.append("Your profile is still my placeholder (a Copenhagen backend "
                        "engineer). If that isn't you, nothing will match.")

    kw = cfg.get("affinity", {}).get("keywords", {})
    example_kw = {r"\bpython\b", r"\bgo\b|\bgolang\b", "postgres|postgresql"}
    if example_kw & set(kw):
        print(f"{WARN}affinity keywords are still the example backend-engineer set")
        verdicts.append("affinity: keywords still list Python/Go/Postgres. These must "
                        "describe YOUR field or every posting scores below min_score.")

    # ----------------------------------------------------------------- state
    hr("2. STATE (have these been seen before?)")
    if STATE.exists():
        import json
        seen = set(json.loads(STATE.read_text()).get("seen", []))
        print(f"{OK}{len(seen)} postings already recorded as seen")
        if len(seen) > 50:
            print("      A normal run only shows postings NEW since last time.")
            print("      An empty board can simply mean nothing new today.")
    else:
        seen = set()
        print(f"{OK}no state yet — this is a first run, everything counts as new")

    # ---------------------------------------------------------------- fetch
    hr("3. FETCH (can we reach each source?)")
    all_jobs, live_sources, dead = [], [], []

    for entry in companies:
        fetcher = sources.ATS_FETCHERS.get(entry["ats"])
        name = entry.get("name", entry["slug"])
        if not fetcher:
            print(f"{BAD}{name:<20} unknown ats '{entry['ats']}'")
            dead.append(name)
            continue
        try:
            found = fetcher(entry["slug"], name)
            status = OK if found else WARN
            print(f"{status}{name:<20} {len(found):>4} postings  "
                  f"({entry['ats']}/{entry['slug']})")
            if found:
                live_sources.append(name)
            else:
                dead.append(f"{name} (reachable but empty)")
            all_jobs.extend(found)
        except Exception as exc:
            msg = str(exc).split(" for url")[0]
            print(f"{BAD}{name:<20} {msg}")
            dead.append(f"{name} ({msg[:40]})")

    for search in searches:
        fetcher = sources.BOARD_FETCHERS.get(search.get("board"))
        label = f"{search.get('board')}/{search.get('label', '?')}"
        if not fetcher:
            print(f"{BAD}{label:<20} unknown board")
            continue
        params = {k: v for k, v in search.items() if k not in ("board", "label")}
        try:
            found = fetcher(params)
            print(f"{OK if found else WARN}{label:<20} {len(found):>4} postings")
            all_jobs.extend(found)
            if found:
                live_sources.append(label)
        except Exception as exc:
            print(f"{BAD}{label:<20} {str(exc).split(' for url')[0]}")
            dead.append(label)

    if not all_jobs:
        verdicts.append("NOTHING WAS FETCHED. Every source failed or returned empty, "
                        "so no filter is to blame. Fix the sources first — a 404 "
                        "usually means a wrong ats/slug pair in config.yaml.")
        report(verdicts)
        return 1

    print(f"\n      {len(all_jobs)} postings fetched in total")

    hr("4. WHAT THE SOURCES ACTUALLY RETURNED")
    print("Check these locations against your location_include rules:\n")
    for job in all_jobs[:8]:
        print(f"  {job['title'][:40]:<40} | {job.get('location', '')[:24]:<24} "
              f"| {job['source']}")

    # ------------------------------------------------------------ each rule
    hr("5. RULES (which one is doing the damage?)")
    deduped = rank.merge(all_jobs)
    print(f"      {len(deduped)} after cross-board dedup\n")

    rules = cfg.get("rules", {})
    survivors = deduped
    for label, key, field in [
        ("title_include", "title_include", "title"),
        ("title_exclude", "title_exclude", "title"),
        ("location_include", "location_include", "location"),
        ("description_exclude", "description_exclude", "description"),
    ]:
        pats = compile_patterns(rules.get(key))
        if not pats:
            print(f"{OK}{label:<22} (not set, nothing dropped)")
            continue
        before = len(survivors)
        if "include" in key:
            survivors = [j for j in survivors
                         if any(p.search(j.get(field) or j.get("excerpt", "")) for p in pats)]
        else:
            survivors = [j for j in survivors
                         if not any(p.search(j.get(field) or j.get("excerpt", "")) for p in pats)]
        killed = before - len(survivors)
        flag = BAD if len(survivors) == 0 and before > 0 else OK
        print(f"{flag}{label:<22} dropped {killed:>4} → {len(survivors):>4} left")
        if len(survivors) == 0 and before > 0:
            verdicts.append(f"rules.{key} rejected ALL {before} remaining postings. "
                            f"This is your problem. Compare its patterns against the "
                            f"locations/titles printed in section 4.")
            report(verdicts)
            return 1

    # --------------------------------------------------------------- ranking
    hr("6. AFFINITY RANKING")
    conf = cfg.get("affinity", {})
    weights = rank.compile_weights(conf.get("keywords"))
    floor = conf.get("min_score", 0)
    for job in survivors:
        job["affinity"] = rank.affinity(job, weights, conf.get("title_boost", 3.0))
    survivors.sort(key=lambda j: j["affinity"], reverse=True)
    clearing = [j for j in survivors if j["affinity"] >= floor]

    print(f"      min_score is {floor}. Top scores from your {len(survivors)} candidates:\n")
    for job in survivors[:8]:
        mark = "✓" if job["affinity"] >= floor else "✗"
        print(f"  {mark} {job['affinity']:>6.1f}  {job['company'][:16]:<16} {job['title'][:36]}")

    if not clearing:
        best = survivors[0]["affinity"] if survivors else 0
        verdicts.append(f"No posting reached min_score ({floor}); the best scored "
                        f"{best:.1f}. Either your affinity keywords don't describe "
                        f"these jobs, or min_score is too high. Try lowering it.")
    else:
        print(f"\n{OK}{len(clearing)} would go to the model")
        threshold = cfg.get("display_threshold", 5.0)
        verdicts.append(f"Pipeline looks healthy: {len(clearing)} postings would be "
                        f"scored. If the site is still empty, the model scored them "
                        f"all below display_threshold ({threshold}). Lower it to 3 "
                        f"to see everything, then raise it once you trust the scores.")

    report(verdicts)
    return 0


def report(verdicts):
    hr("DIAGNOSIS")
    if not verdicts:
        print("  Nothing obviously wrong.")
    for i, v in enumerate(verdicts, 1):
        print(f"  {i}. {v}\n")


if __name__ == "__main__":
    sys.exit(main())
