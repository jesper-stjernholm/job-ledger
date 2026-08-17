"""
Job sources.

Two kinds, and the difference matters:

  ATS boards (greenhouse, lever, ashby, teamtailor)
      Authoritative, immediate, low volume. A posting appears here first.
      You must know the company in advance.

  Aggregators (himalayas, remotive, remoteok)
      Broad discovery, high volume, 24h+ lag, duplicated across boards.
      You don't need to know anyone in advance.

Use aggregators to *find companies*, then move the good ones onto the ATS
list — see the discovery loop in agent.py.
"""
from __future__ import annotations

import html as htmllib
import re
import sys
import time

import requests

TIMEOUT = 30
UA = {"User-Agent": "personal-job-agent/1.0 (+https://github.com)"}

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = htmllib.unescape(raw)
    text = _BLOCK.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _qs(params: dict) -> dict:
    """
    Python renders True as "True"; these APIs expect "true". YAML booleans in
    config.yaml arrive here as real bools, so normalise before sending.
    """
    out = {}
    for key, value in (params or {}).items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, list):
            out[key] = [("true" if v is True else "false" if v is False else v) for v in value]
        else:
            out[key] = value
    return out


def _get(url: str, params: dict | None = None):
    resp = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
    resp.raise_for_status()
    return resp.json()


# ============================================================== ATS (targeted)

def fetch_greenhouse(slug: str, company: str) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [{
        "uid": f"greenhouse:{slug}:{j.get('id')}",
        "company": company,
        "title": (j.get("title") or "").strip(),
        "location": (j.get("location") or {}).get("name", "").strip(),
        "url": j.get("absolute_url", ""),
        "excerpt": strip_html(j.get("content"))[:400],
        "description": strip_html(j.get("content")),
        "comp": "",
        "source": "greenhouse",
    } for j in data.get("jobs", [])]


def fetch_lever(slug: str, company: str) -> list[dict]:
    """
    Lever shards some accounts onto a region-specific host — the public
    board for these is jobs.eu.lever.co/{slug} rather than jobs.lever.co/
    {slug}, and the API mirrors that split. Try the default host first
    (covers the large majority), fall back to the EU host on a 404 rather
    than silently missing every EU-sharded company.
    """
    try:
        data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            data = _get(f"https://api.eu.lever.co/v0/postings/{slug}?mode=json")
        else:
            raise
    out = []
    for j in data:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or ""]
        for lst in (j.get("lists") or []):
            parts.append(f"{lst.get('text', '')}: {strip_html(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        desc = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
        out.append({
            "uid": f"lever:{slug}:{j.get('id')}",
            "company": company,
            "title": (j.get("text") or "").strip(),
            "location": (cats.get("location") or "").strip(),
            "url": j.get("hostedUrl") or j.get("applyUrl", ""),
            "excerpt": desc[:400],
            "description": desc,
            "comp": _lever_salary(j.get("salaryRange")),
            "source": "lever",
        })
    return out


def _lever_salary(rng: dict | None) -> str:
    if not rng or not rng.get("min"):
        return ""
    low, high = rng["min"], rng.get("max")
    span = f"{low:,.0f}" if not high or high == low else f"{low:,.0f}-{high:,.0f}"
    return f"{rng.get('currency', '')} {span}".strip()


def fetch_ashby(slug: str, company: str) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml"))
        out.append({
            "uid": f"ashby:{slug}:{j.get('id') or j.get('jobUrl')}",
            "company": company,
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "url": j.get("jobUrl", ""),
            "excerpt": desc[:400],
            "description": desc,
            "comp": (j.get("compensation") or {}).get("compensationTierSummary") or "",
            "source": "ashby",
        })
    return out


# Teamtailor's jobLocation.address.addressCountry is an ISO-3166 alpha-2
# code ("DK"), not a word - a location_include pattern like "denmark" or
# "europe" would silently never match it. Resolve to a full name and, for
# EU/EEA members, append "Europe" too, so it matches the same way a hand-
# written "Copenhagen, Denmark, Europe" string from another source would.
_EU_EEA_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}
_COUNTRY_NAMES = {
    "DK": "Denmark", "SE": "Sweden", "NO": "Norway", "FI": "Finland",
    "IS": "Iceland", "DE": "Germany", "FR": "France", "NL": "Netherlands",
    "GB": "United Kingdom", "IE": "Ireland", "ES": "Spain", "IT": "Italy",
    "PT": "Portugal", "PL": "Poland", "AT": "Austria", "BE": "Belgium",
    "CH": "Switzerland", "AX": "Aland Islands", "US": "United States",
    "CA": "Canada",
}


def _teamtailor_location(jp: dict) -> str:
    places = []
    for place in jp.get("jobLocation") or []:
        addr = place.get("address") or {}
        code = addr.get("addressCountry") or ""
        country = _COUNTRY_NAMES.get(code, code)
        parts = [addr.get("addressLocality"), country]
        if code in _EU_EEA_COUNTRIES:
            parts.append("Europe")
        places.append(", ".join(p for p in parts if p))
    return "; ".join(p for p in places if p)


def fetch_teamtailor(slug: str, company: str) -> list[dict]:
    """
    Every Teamtailor career site (both *.teamtailor.com subdomains and
    custom domains like jobs.company.com) publishes a public, unauthenticated
    JSON Feed at /jobs.json - no API key, same shape regardless of domain.
    Unlike the other three adapters, `slug` here is the career site's full
    hostname, not a short identifier, since Teamtailor sites can live on
    either kind of domain.
    """
    data = _get(f"https://{slug}/jobs.json")
    out = []
    for item in data.get("items", []):
        jp = item.get("_jobposting") or {}
        desc = strip_html(item.get("content_html"))
        out.append({
            "uid": f"teamtailor:{slug}:{item.get('id')}",
            "company": company,
            "title": (item.get("title") or "").strip(),
            "location": _teamtailor_location(jp),
            "url": item.get("url", ""),
            "excerpt": desc[:400],
            "description": desc,
            "comp": "",
            "source": "teamtailor",
        })
    return out


# ======================================================= Aggregators (discovery)

def fetch_himalayas(query: dict) -> list[dict]:
    """
    https://himalayas.app/docs/remote-jobs-api — free, no key, refreshed daily.

    Use /jobs/api/search rather than the browse feed: browse caps at limit=20
    per request, so crawling it whole means hundreds of calls for data you
    would immediately throw away. Server-side filters are free; a posting you
    never download costs nothing to reject.

    Params: q, country, worldwide, seniority, employment_type, company,
    timezone, sort, page.
    """
    data = _get("https://himalayas.app/jobs/api/search", params=_qs(query))
    out = []
    for j in data.get("jobs", []):
        countries = [c.get("name", "") for c in (j.get("locationRestrictions") or [])]
        location = ", ".join(countries) if countries else "Worldwide"
        salary = ""
        if j.get("minSalary"):
            hi = j.get("maxSalary")
            salary = f"{j.get('currency', '')} {j['minSalary']:,.0f}"
            if hi and hi != j["minSalary"]:
                salary += f"-{hi:,.0f}"
        out.append({
            "uid": f"himalayas:{j.get('guid')}",
            "company": (j.get("companyName") or "").strip(),
            "company_slug": j.get("companySlug", ""),
            "title": (j.get("title") or "").strip(),
            "location": location,
            "url": j.get("applicationLink", ""),
            # excerpt is a short plain-text summary — the whole point of the
            # two-stage rank. Ranking reads this; only finalists cost a full
            # description.
            "excerpt": (j.get("excerpt") or "")[:400],
            "description": strip_html(j.get("description")),
            "comp": salary,
            "seniority": ", ".join(j.get("seniority") or []),
            "source": "himalayas",
        })
    return out


def fetch_remotive(query: dict) -> list[dict]:
    """https://remotive.com/remote-jobs/api — free, no key."""
    data = _get("https://remotive.com/api/remote-jobs", params=_qs(query))
    out = []
    for j in data.get("jobs", []):
        desc = strip_html(j.get("description"))
        out.append({
            "uid": f"remotive:{j.get('id')}",
            "company": (j.get("company_name") or "").strip(),
            "title": (j.get("title") or "").strip(),
            "location": j.get("candidate_required_location", "") or "Worldwide",
            "url": j.get("url", ""),
            "excerpt": desc[:400],
            "description": desc,
            "comp": j.get("salary", "") or "",
            "source": "remotive",
        })
    return out


def fetch_remoteok(query: dict) -> list[dict]:
    """
    https://remoteok.com/api — free JSON feed. The first array element is a
    legal/attribution notice rather than a job; skip it.
    """
    resp = requests.get("https://remoteok.com/api", params=_qs(query),
                        timeout=TIMEOUT, headers=UA)
    resp.raise_for_status()
    rows = resp.json()
    out = []
    for j in rows:
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue
        desc = strip_html(j.get("description"))
        salary = ""
        if j.get("salary_min"):
            salary = f"USD {j['salary_min']:,.0f}"
            if j.get("salary_max"):
                salary += f"-{j['salary_max']:,.0f}"
        out.append({
            "uid": f"remoteok:{j.get('id')}",
            "company": (j.get("company") or "").strip(),
            "title": (j.get("position") or "").strip(),
            "location": j.get("location", "") or "Worldwide",
            "url": j.get("url", ""),
            "excerpt": (" ".join(j.get("tags") or []) + " " + desc)[:400],
            "description": desc,
            "comp": salary,
            "source": "remoteok",
        })
    return out


ATS_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
                "teamtailor": fetch_teamtailor}
BOARD_FETCHERS = {"himalayas": fetch_himalayas, "remotive": fetch_remotive,
                  "remoteok": fetch_remoteok}

# Boards asking for visible credit when their data is displayed. Rendered in
# the page footer — it costs nothing and it is the deal these free APIs offer.
ATTRIBUTION = {
    "himalayas": ("Himalayas", "https://himalayas.app"),
    "remotive": ("Remotive", "https://remotive.com"),
    "remoteok": ("Remote OK", "https://remoteok.com"),
}


def collect(companies: list[dict], searches: list[dict], polite_delay: float = 1.0):
    """Fetch everything. A dead source warns and is skipped, never fatal."""
    jobs, used_boards = [], set()

    print("ATS boards:")
    for entry in companies:
        fetcher = ATS_FETCHERS.get(entry["ats"])
        name = entry.get("name", entry["slug"])
        if not fetcher:
            print(f"  ! unknown ats '{entry['ats']}' for {name}", file=sys.stderr)
            continue
        try:
            found = fetcher(entry["slug"], name)
            print(f"  {name:<24} {len(found):>4}")
            jobs.extend(found)
        except Exception as exc:
            print(f"  ! {name}: {exc}", file=sys.stderr)

    if searches:
        print("\nAggregator searches:")
    for search in searches:
        board = search.get("board")
        fetcher = BOARD_FETCHERS.get(board)
        if not fetcher:
            print(f"  ! unknown board '{board}'", file=sys.stderr)
            continue
        params = {k: v for k, v in search.items() if k not in ("board", "label")}
        label = search.get("label") or params.get("q") or params.get("search") or "all"
        try:
            found = fetcher(params)
            print(f"  {board}/{label:<20} {len(found):>4}")
            jobs.extend(found)
            used_boards.add(board)
        except Exception as exc:
            print(f"  ! {board}/{label}: {exc}", file=sys.stderr)
        time.sleep(polite_delay)   # these are free APIs; don't hammer them

    return jobs, used_boards
