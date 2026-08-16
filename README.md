# Job Ledger

A daily job agent that watches company ATS boards, scores new postings against
your profile, and shows you the results on a small password-gated website.

Config and state live in SQLite (`state/job_agent.db`), edited through the
web UI (`web.py`) rather than by hand-editing YAML. Costs about **one cent a
day** in API tokens. Can run entirely on your own machine, or be deployed
(e.g. to Railway) so it's reachable from anywhere — see Setup below.

## How it stays cheap

The model never sees a raw board. Four deterministic stages run first:

| Stage | Runs on | Typical survivors |
|---|---|---|
| Fetch ATS boards + aggregator searches | code | ~900 postings |
| Drop anything already in `state/seen.json` | code | ~120 new |
| Collapse cross-board duplicates | code | ~80 |
| Regex rules on title, location, body | code | ~40 |
| Affinity rank, keep the top N | code | ~25 |
| Score against your profile | **Haiku 4.5** | 25 scored |

The dedup step is the one that matters most. Without it you re-score the same
postings every morning forever; with it, steady-state cost tracks the number of
genuinely new jobs, which is small. Descriptions are truncated to 1,200
characters before scoring — requirements sit near the top of a posting and the
rest is culture boilerplate.

Roughly 12k input and 2k output tokens a day — about $0.02. Scoring runs on
Haiku because "does this match the profile" is an easy classification job.

Note the shape: adding aggregators multiplied raw volume by ~1.5x and new
postings by ~8x, but only doubled cost, because `max_to_model` caps what the
model ever sees. The ranking stage decides *which* 25, and it's free.

## Setup

### Option A: run it locally

No hosting, no account, nothing reachable but your own machine.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="..."      # console.anthropic.com
export PROFILE="$(cat profile.txt)" # your profile — gitignored, never committed
export APP_PASSWORD="..."           # your choice, hashed on first boot
python web.py
```

Open `http://127.0.0.1:8000/`, log in, and use Configuration/Boards to set up
your roles, keywords and company list. Trigger a real run with
`python agent.py --max-cost 1.0` whenever you want fresh scores (a cap is
optional but recommended — see `agent.py --help`); `web.py` also runs one
itself at 06:00 UTC on weekdays while it's running.

### Option B: deploy it (e.g. to Railway)

Same app, reachable from anywhere. You'll need to:

1. Create a Railway account and connect it to this GitHub repo.
2. Attach a persistent volume (any mount path, e.g. `/data`) — the database
   needs real disk, or it resets on every deploy and every run re-scores
   everything, quietly multiplying cost.
3. Set these environment variables in the Railway project:
   - `STATE_DIR` — the volume's mount path (e.g. `/data`)
   - `APP_PASSWORD` — your login password (hashed on first boot, then ignored)
   - `ANTHROPIC_API_KEY`, `PROFILE` — same as local
   - `REQUIRE_HTTPS=1` — Railway terminates real TLS at its edge, so the
     session cookie can safely require it
4. Deploy. `Procfile` tells Railway how to start it (`python web.py`).

The hosted database starts empty — re-enter your roles/keywords/companies
through the UI once it's live. Scheduling runs in-process (see `web.py`'s
`_scheduler_loop`), so there's no separate cron service or shared-volume
question to get wrong.

## Two kinds of source

| | ATS boards | Aggregators |
|---|---|---|
| Examples | Greenhouse, Lever, Ashby | Himalayas, Remotive, RemoteOK |
| Need to know the company? | Yes | No |
| Freshness | Immediate | 24h+ cache |
| Volume | ~15 new/day | Hundreds/day |
| Duplicates | None | Same role on 3-4 boards |
| Good for | Watching places you've chosen | **Finding places you haven't** |

Run both. Aggregators are how you discover companies; ATS boards are how you
monitor them properly once you have.

### The discovery loop

When an aggregator posting scores at or above `discovery_threshold`, its
company gets logged to `state/discovered.json`:

```json
{"Doist": {"slug_hint": "doist", "hits": 3, "best_score": 8.4,
           "example": "Senior Backend Engineer — https://...",
           "last_seen": "2026-08-13"}}
```

Skim it weekly. Companies that keep surfacing good roles belong in
`companies:`, where you'll see their postings the day they go up instead of a
day or two later through a cache. Over a month or so the curated list becomes
your high-signal channel and the aggregators become pure discovery.

### Why aggregators need two extra free stages

They invert the funnel — hundreds of new postings a day instead of fifteen.
Two deterministic stages absorb that before anything reaches the model:

**Cross-source dedup** (`rank.merge`). The same role appears on Himalayas and
Remotive and RemoteOK. Matching on normalised company + title collapses them
into one record, keeps the ATS version when there is one (richest description),
backfills missing salary from whichever board published it, and records the
rest in `also_on` — the small badges under each posting.

**Affinity ranking** (`rank.rank`). Weighted regex over title + short excerpt,
scored before any full description is fetched or sent. Each pattern counts once
so repetition can't game it; a title hit is worth `title_boost` times a body
hit; negative weights sink crypto, PHP, internships without spending a token
explaining them. Only the top `max_to_model` survive.

Tune `affinity:`, not the prompt. It runs free and `--dry-run` prints every
score so you can see exactly why something ranked where it did.

### Rate limits and attribution

All three boards are free and unauthenticated. In return:

- Poll **once a day**. Himalayas caches for 24h — polling more often returns
  identical data and risks a 429. `polite_delay` spaces out the calls.
- Prefer `searches:` filters over crawling. Server-side filtering is the
  cheapest stage in the whole pipeline: a posting you never download costs
  nothing to reject.
- **Credit them.** All three ask for a visible link back. The footer does this
  automatically for whichever boards a run actually used. Leave it in.

## Finding company slugs

Open a company's careers page and read the URL:

| URL | `ats` | `slug` |
|---|---|---|
| `boards.greenhouse.io/monzo` | `greenhouse` | `monzo` |
| `jobs.lever.co/plaid` | `lever` | `plaid` |
| `jobs.ashbyhq.com/linear` | `ashby` | `linear` |

All three are public, unauthenticated, documented endpoints. Some companies
disable the public feed; those show up as a warning line in the run log and are
skipped.

Between them these cover a large share of tech hiring. Workable, SmartRecruiters
and Recruitee also have public feeds if you need them — add a `fetch_*` function
and register it in `FETCHERS`.

## Writing a good profile

Around 150 words. What helps:

- Years of experience and the seniority band you want
- Core stack, named specifically
- Location and work authorisation
- Explicit dealbreakers

What hurts: a pasted CV. Employment history dilutes the signal and costs tokens
on every batch.

## Tuning

Run `python agent.py --dry-run` locally. It fetches and filters but makes no API
call and writes no state, so you can iterate on the regex rules for free until
the survivor list looks right. Check the *rejects* too — over-tight rules fail
silently, and the most common cause is a `location_include` pattern listing
continents when ATS fields actually hold city strings like `London, UK`.

Once scores are flowing, the numbers themselves are the feedback: if everything
lands at 6–7, the profile is too vague to discriminate. Sharpen the dealbreakers
before touching the prompt.

## Files

```
agent.py            pipeline: fetch, filter, score, render — CLI, --dry-run, --max-cost
doctor.py           stage-by-stage diagnostic, no API calls, no writes
web.py              the web UI: board, runs, configuration, boards, discovered
db.py               SQLite schema + all reads/writes; state/job_agent.db is the db
sources.py, rank.py fetchers and dedup/ranking — unchanged since v1
migrate.py          one-time YAML/JSON -> SQLite migration (historical; already run)
config.yaml         kept as historical reference only, no longer read at runtime
```

`config.yaml` isn't live config any more — companies, rules, keywords and
thresholds all live in `state/job_agent.db` and are edited through the web UI
(`/config`, `/boards`) instead.

## Cost guard

`max_to_model` (Configuration page, default 25) caps how many postings reach
the model in one run — the ranking stage decides *which* ones, for free, so a
company dumping 200 openings one morning still only costs you 25. Independently,
`python agent.py --max-cost 1.0` caps actual USD spend for a single run,
checked against real API usage between batches, not an estimate.
