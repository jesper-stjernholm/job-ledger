"""
Cross-source dedup and free affinity ranking.

Aggregators invert the funnel. Thirty curated ATS boards yield ~15 new
postings a day and you can afford to score nearly all of them. Aggregators
yield hundreds, heavily duplicated, and you cannot. So two free stages sit
between the rules and the model:

  1. merge()  — the same role appears on Himalayas AND Remotive AND RemoteOK.
                Scoring it three times costs 3x for nothing.
  2. affinity() — a transparent weighted-keyword score over title + excerpt.
                Only the top N get their full description sent to the model.

Both run on plain Python in milliseconds and cost nothing. The model is the
last stage, not the filter.
"""
from __future__ import annotations

import re

# Legal suffixes that differ between boards for the same employer.
_LEGAL = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|bv|nv|ab|as|a/s|aps|oy|plc|corp|co|sa|srl|"
    r"pte|pty|holdings|group|technologies|technology|labs|software)\b\.?", re.I)

# Noise boards bolt onto titles.
_TITLE_NOISE = re.compile(
    r"\(.*?\)|\[.*?\]|\bm/f/d\b|\bm/w/d\b|\bh/f\b|\bremote\b|\bhybrid\b|"
    r"\bfull[- ]time\b|\bpart[- ]time\b|\bcontract\b|\bfreelance\b|"
    r"\bus\b|\beu\b|\buk\b|\bemea\b", re.I)


def canon_company(name: str) -> str:
    name = _LEGAL.sub(" ", name or "")
    return re.sub(r"[^a-z0-9]", "", name.lower())


def canon_title(title: str) -> str:
    title = _TITLE_NOISE.sub(" ", title or "")
    title = re.split(r"\s+[-–—|@]\s+", title)[0]      # "Engineer - Berlin" -> "Engineer"
    return re.sub(r"[^a-z0-9]", "", title.lower())


def merge(jobs: list[dict]) -> list[dict]:
    """
    Collapse duplicates on (company, title). Keeps the record with the longest
    description — ATS entries usually beat aggregator entries — and records
    every board it appeared on in `also_on`.
    """
    buckets: dict[tuple, list[dict]] = {}
    for job in jobs:
        key = (canon_company(job.get("company", "")), canon_title(job.get("title", "")))
        if not any(key):
            key = (job.get("uid", ""),)
        buckets.setdefault(key, []).append(job)

    merged = []
    for group in buckets.values():
        # Prefer ATS over aggregator, then the richest description.
        group.sort(key=lambda j: (
            j.get("source") in ("greenhouse", "lever", "ashby", "teamtailor", "nordea"),
            len(j.get("description", "")),
        ), reverse=True)
        best = dict(group[0])
        others = {j.get("source") for j in group[1:]} - {best.get("source")}
        best["also_on"] = sorted(s for s in others if s)
        # Fill blanks from the duplicates — boards differ in what they publish.
        for field in ("comp", "location", "seniority", "company_slug"):
            if not best.get(field):
                for other in group[1:]:
                    if other.get(field):
                        best[field] = other[field]
                        break
        merged.append(best)
    return merged


def compile_weights(raw: dict) -> list[tuple[re.Pattern, float]]:
    return [(re.compile(pattern, re.I), float(weight)) for pattern, weight in (raw or {}).items()]


def affinity(job: dict, weights: list[tuple[re.Pattern, float]], title_boost: float) -> float:
    """
    Weighted keyword score over title and excerpt.

    Each pattern counts at most once, so a description that repeats "Python"
    twelve times does not outrank a genuinely better match. A hit in the title
    is worth `title_boost` times a hit in the body — titles are far more
    informative than the benefits paragraph. Negative weights push things down.
    """
    title = job.get("title", "")
    body = f"{job.get('excerpt', '')} {job.get('seniority', '')}"
    score = 0.0
    for pattern, weight in weights:
        if pattern.search(title):
            score += weight * title_boost
        elif pattern.search(body):
            score += weight
    return score


def rank(jobs: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Score, sort, split into what reaches the model and what doesn't."""
    conf = cfg.get("affinity", {})
    weights = compile_weights(conf.get("keywords"))
    title_boost = conf.get("title_boost", 3.0)
    floor = conf.get("min_score", 0)
    cap = conf.get("max_to_model", 25)

    for job in jobs:
        job["affinity"] = affinity(job, weights, title_boost)

    jobs.sort(key=lambda j: j["affinity"], reverse=True)
    eligible = [j for j in jobs if j["affinity"] >= floor]
    return eligible[:cap], eligible[cap:]
