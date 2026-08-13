# Job Ledger

A daily job agent that watches company ATS boards, scores new postings against
your profile, and publishes a small website.

Runs on GitHub Actions (free), publishes to GitHub Pages (free), and costs
about **one cent a day** in API tokens.

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

**1. Create the repo.** Push these files to a new GitHub repository.

**2. Add two secrets** under Settings → Secrets and variables → Actions:

- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `PROFILE` — your profile text (see below). Keeping it here rather than in
  `config.yaml` means your CV never enters the repo.

**3. Enable Pages** under Settings → Pages: source `Deploy from a branch`,
branch `main`, folder `/docs`. Your board appears at
`https://<user>.github.io/<repo>/`.

**4. Edit `config.yaml`** — the company list and the rules. Then run it by hand
once from the Actions tab (`Run workflow`) before trusting the schedule.

### Privacy

GitHub Pages on a free account requires a **public** repo. The `PROFILE` secret
keeps your CV out of it, and the page carries a `noindex` tag, but the URL is
still guessable and the site does reveal that you're looking. If that matters:

- GitHub Pro ($4/mo) enables Pages on private repos, or
- point Cloudflare Pages at a private repo (free tier, supports private repos),
  and put Cloudflare Access in front of it, or
- drop the Pages step and just `git pull` and open `docs/index.html` locally.

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
agent.py                     pipeline: fetch, filter, score, render
config.yaml                  companies, rules, thresholds, cost guards
state/seen.json              dedup set + the live board (written by the run)
docs/index.html              the published page (sample output committed)
.github/workflows/daily.yml  06:00 UTC weekdays, plus manual trigger
```

`docs/index.html` currently holds a sample render with fake postings, so you can
see the layout before wiring up your API key. The first real run overwrites it.

## Cost guard

`max_scored_per_run` (default 40) caps a single run. If a company dumps 200
openings one morning, you score 40 and pick the rest up over following days
rather than paying for all of them at once.
