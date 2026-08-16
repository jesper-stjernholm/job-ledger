#!/usr/bin/env python3
"""
Configuration + Boards UI, password-gated.

    APP_PASSWORD=your-password python web.py
    -> http://127.0.0.1:8000/

The password is hashed (argon2) and stored via db.set_auth() on first
boot; once a hash exists, APP_PASSWORD is ignored on later boots. Session
cookies are signed but NOT marked Secure unless REQUIRE_HTTPS=1 is set —
turn that on (behind real TLS) before this is reachable from anywhere but
your own machine or localhost.

Runs its own daily scheduler in-process (see `_scheduler_loop` below)
rather than relying on an external cron, so the web UI and the scoring
run always agree on which database they're using — see the phase's plan
for why that matters once this is hosted with a persistent volume shared
by nothing else.
"""
import asyncio
import hmac
import logging
import os
import secrets
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import agent
import db
import sources

ROOT = Path(__file__).parent
ph = PasswordHasher()

# Without this, log.info() below is silently dropped (the root logger's
# default level is WARNING) — which would make the scheduler's activity
# invisible in a hosted platform's log viewer, exactly where it matters
# most, since nobody's watching a local terminal there.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

# 06:00 UTC weekdays — the same schedule the retired daily.yml GitHub
# Actions workflow used.
SCHEDULE_HOUR_UTC = 6
SCHEDULE_WEEKDAYS = {0, 1, 2, 3, 4}  # Monday-Friday

# ------------------------------------------------------------- auth setup
# Runs once at import time, before SessionMiddleware is attached, so the
# signing secret is stable across restarts (persisted in state/auth.local.json
# — gitignored, NOT job_agent.db, since that file is committed to a public
# repo and a leaked signing secret would let anyone forge a login cookie)
# rather than regenerated, which would silently log everyone out on every
# restart.
with db.connect() as _conn:
    db.init_schema(_conn)
_auth = db.get_auth()
_session_secret = _auth.get("session_secret") or secrets.token_hex(32)
if not _auth.get("session_secret"):
    db.set_auth(session_secret=_session_secret)
if not _auth.get("password_hash") and os.environ.get("APP_PASSWORD"):
    db.set_auth(password_hash=ph.hash(os.environ["APP_PASSWORD"]))

app = FastAPI()
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["css"] = agent.CSS

NAV = (
    '<div class="nav">'
    '<a href="/" aria-current="page">Board</a>'
    '<a href="/runs">Runs</a>'
    '<a href="/config">Configuration</a>'
    '<a href="/boards">Boards</a>'
    '<a href="/discovered">Discovered</a>'
    '<a href="/logout" style="margin-left:auto">Log out</a>'
    "</div>"
)

# --------------------------------------------------------- login rate limit

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
_login_attempts: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < WINDOW_SECONDS]
    _login_attempts[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def _record_failure(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def _clear_failures(ip: str) -> None:
    _login_attempts.pop(ip, None)


SESSION_EXEMPT_PATHS = {"/login", "/logout", "/api/boards/bulk_add"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path not in SESSION_EXEMPT_PATHS and not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Starlette's add_middleware() prepends to the stack, and the LAST-added
# entry ends up outermost (runs first on the way in). SessionMiddleware
# must run before require_login's dispatch touches request.session, so it
# has to be added AFTER require_login is registered above, not before.
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=os.environ.get("REQUIRE_HTTPS") == "1",
    max_age=60 * 60 * 24 * 14,  # 14 days
)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    configured = bool(db.get_auth().get("password_hash"))
    return templates.TemplateResponse(request, "login.html", {
        "configured": configured, **flash_ctx(request),
    })


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    password_hash = db.get_auth().get("password_hash", "")

    if not password_hash:
        return redirect_with_flash(
            "/login", "No password configured yet — set APP_PASSWORD and restart.", error=True)

    if _rate_limited(ip):
        return redirect_with_flash(
            "/login", "Too many failed attempts. Wait a few minutes and try again.", error=True)

    try:
        ph.verify(password_hash, password)
    except VerifyMismatchError:
        _record_failure(ip)
        return redirect_with_flash("/login", "Wrong password.", error=True)

    _clear_failures(ip)
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def flash_ctx(request: Request) -> dict:
    return {
        "flash": request.query_params.get("flash"),
        "flash_error": request.query_params.get("error") == "1",
    }


def redirect_with_flash(path: str, message: str, error: bool = False) -> RedirectResponse:
    qs = urlencode({"flash": message, "error": "1" if error else "0"})
    return RedirectResponse(f"{path}?{qs}", status_code=303)


# ------------------------------------------------------------------- board

@app.get("/", response_class=HTMLResponse)
def board_page():
    with db.connect() as conn:
        db.init_schema(conn)
        settings = db.get_settings(conn)
        today = date.today().isoformat()
        board_rows = db.get_board(conn, settings["retain_days"], today)
        board = [agent.Job(**{k: v for k, v in r.items() if k in agent.Job.__annotations__})
                 for r in board_rows]
        last_run = conn.execute("SELECT cost_usd FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        cost = last_run["cost_usd"] if last_run else 0.0
        all_boards = db.list_boards(conn)

    watched = sum(1 for b in all_boards if b["enabled"] and b["adapter"] in ("greenhouse", "lever", "ashby"))
    used_boards = {b["adapter"] for b in all_boards if b["enabled"] and b["adapter"] in sources.ATTRIBUTION}
    html = agent.render(board, settings, cost, watched, used_boards, nav_html=NAV)
    return HTMLResponse(html)


# -------------------------------------------------------------------- runs

@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    with db.connect() as conn:
        db.init_schema(conn)
        runs = db.get_runs(conn)
    return templates.TemplateResponse(request, "runs.html", {
        "active": "runs", "runs": runs, **flash_ctx(request),
    })


# ------------------------------------------------------------------ config

@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    with db.connect() as conn:
        db.init_schema(conn)
        settings = db.get_settings(conn)
        roles = db.list_roles(conn)
        exclusions = db.list_exclusions(conn)
        keywords = db.list_keywords(conn)
    return templates.TemplateResponse(request, "config.html", {
        "active": "config",
        "settings": settings, "roles": roles, "exclusions": exclusions, "keywords": keywords,
        "profile_env_set": bool(os.environ.get("PROFILE")),
        **flash_ctx(request),
    })


@app.post("/config/settings")
def config_settings(
    profile_text: str | None = Form(None),
    site_title: str | None = Form(None),
    display_threshold: float | None = Form(None),
    highlight_threshold: float | None = Form(None),
    min_affinity: float | None = Form(None),
    max_to_model: int | None = Form(None),
    description_chars: int | None = Form(None),
    retain_days: int | None = Form(None),
    discovery_threshold: float | None = Form(None),
    batch_size: int | None = Form(None),
):
    fields = {k: v for k, v in {
        "profile_text": profile_text, "site_title": site_title,
        "display_threshold": display_threshold, "highlight_threshold": highlight_threshold,
        "min_affinity": min_affinity, "max_to_model": max_to_model,
        "description_chars": description_chars, "retain_days": retain_days,
        "discovery_threshold": discovery_threshold, "batch_size": batch_size,
    }.items() if v is not None}
    with db.connect() as conn:
        db.init_schema(conn)
        db.update_settings(conn, **fields)
    return redirect_with_flash("/config", "Saved.")


@app.post("/roles/add")
def roles_add(term: str = Form(...)):
    with db.connect() as conn:
        db.init_schema(conn)
        db.add_role(conn, term.strip())
    return redirect_with_flash("/config", f'Added role "{term.strip()}".')


@app.post("/roles/{role_id}/toggle")
def roles_toggle(role_id: int):
    with db.connect() as conn:
        db.toggle_role(conn, role_id)
    return RedirectResponse("/config", status_code=303)


@app.post("/roles/{role_id}/delete")
def roles_delete(role_id: int):
    with db.connect() as conn:
        db.delete_role(conn, role_id)
    return RedirectResponse("/config", status_code=303)


@app.post("/exclusions/add")
def exclusions_add(term: str = Form(...), scope: str = Form("title")):
    with db.connect() as conn:
        db.init_schema(conn)
        db.add_exclusion(conn, term.strip(), scope)
    return redirect_with_flash("/config", f'Added exclusion "{term.strip()}".')


@app.post("/exclusions/{exclusion_id}/toggle")
def exclusions_toggle(exclusion_id: int):
    with db.connect() as conn:
        db.toggle_exclusion(conn, exclusion_id)
    return RedirectResponse("/config", status_code=303)


@app.post("/exclusions/{exclusion_id}/delete")
def exclusions_delete(exclusion_id: int):
    with db.connect() as conn:
        db.delete_exclusion(conn, exclusion_id)
    return RedirectResponse("/config", status_code=303)


@app.post("/keywords/add")
def keywords_add(term: str = Form(...), weight: float = Form(...)):
    with db.connect() as conn:
        db.init_schema(conn)
        db.add_keyword(conn, term.strip(), weight)
    return redirect_with_flash("/config", f'Added keyword "{term.strip()}".')


@app.post("/keywords/{keyword_id}/toggle")
def keywords_toggle(keyword_id: int):
    with db.connect() as conn:
        db.toggle_keyword(conn, keyword_id)
    return RedirectResponse("/config", status_code=303)


@app.post("/keywords/{keyword_id}/delete")
def keywords_delete(keyword_id: int):
    with db.connect() as conn:
        db.delete_keyword(conn, keyword_id)
    return RedirectResponse("/config", status_code=303)


# ----------------------------------------------------------------- boards

@app.get("/boards", response_class=HTMLResponse)
def boards_page(request: Request):
    with db.connect() as conn:
        db.init_schema(conn)
        all_boards = db.list_boards(conn)
        companies = [b for b in all_boards if b["adapter"] in ("greenhouse", "lever", "ashby")]
        searches = db.list_searches(conn)
    return templates.TemplateResponse(request, "boards.html", {
        "active": "boards",
        "companies": companies, "searches": searches,
        "prefill_name": request.query_params.get("prefill_name", ""),
        "prefill_slug": request.query_params.get("prefill_slug", ""),
        **flash_ctx(request),
    })


def _try_add_board(name: str, adapter: str, slug: str) -> tuple[bool, str]:
    """
    Test-fetches with the real, hardcoded ATS adapter before saving. This is
    NOT the phase-4 arbitrary-URL board tester — adapter and URL template are
    both fixed and already trusted (sources.ATS_FETCHERS), so there's no SSRF
    surface here, just "does this slug actually resolve." Shared by the
    single-company and bulk-add routes.
    """
    name, slug = name.strip(), slug.strip()
    fetcher = sources.ATS_FETCHERS.get(adapter)
    if not fetcher:
        return False, f"{name}: unknown ATS '{adapter}'"
    try:
        found = fetcher(slug, name)
    except Exception as exc:
        return False, f"{name} ({adapter}/{slug}): {exc}"
    if not found:
        return False, f"{name} ({adapter}/{slug}): reachable but 0 postings — check the slug"

    with db.connect() as conn:
        db.init_schema(conn)
        db.add_board(conn, name, adapter, slug)
        db.delete_discovered(conn, name)  # no-op if it wasn't a discovered row
    example = found[0].get("title", "")
    return True, f'{name}: added — found {len(found)} posting(s), e.g. "{example}"'


@app.post("/boards/add")
def boards_add(name: str = Form(...), adapter: str = Form(...), slug: str = Form(...)):
    ok, message = _try_add_board(name, adapter, slug)
    return redirect_with_flash("/boards", message, error=not ok)


@app.post("/boards/bulk_add")
def boards_bulk_add(request: Request, companies: str = Form(...)):
    """
    One line per company: `name, ats, slug`. Processes every line
    synchronously (each is one real test-fetch, same as the single-company
    form) and renders the results inline rather than a single flash message,
    since a batch has more than one outcome to show.
    """
    results = []
    for line in companies.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            results.append({"ok": False, "message": f"'{line}': expected \"name, ats, slug\""})
            continue
        name, adapter, slug = parts
        ok, message = _try_add_board(name, adapter.lower(), slug)
        results.append({"ok": ok, "message": message})

    with db.connect() as conn:
        db.init_schema(conn)
        all_boards = db.list_boards(conn)
        companies_list = [b for b in all_boards if b["adapter"] in ("greenhouse", "lever", "ashby")]
        searches = db.list_searches(conn)
    return templates.TemplateResponse(request, "boards.html", {
        "active": "boards", "companies": companies_list, "searches": searches,
        "prefill_name": "", "prefill_slug": "", "bulk_results": results,
    })


@app.post("/api/boards/bulk_add")
async def api_boards_bulk_add(request: Request):
    """
    Token-authenticated equivalent of the /boards/bulk_add form, for
    automation (e.g. Claude adding researched companies directly) without
    ever touching the session-cookie login. Exempted from require_login
    above; auth here is a separate bearer token, checked in constant time.
    Fails closed if ADMIN_API_TOKEN isn't configured at all.

    Body: {"companies": [{"name": "...", "ats": "...", "slug": "..."}, ...]}
    """
    expected = os.environ.get("ADMIN_API_TOKEN")
    if not expected:
        return JSONResponse({"error": "ADMIN_API_TOKEN not configured"}, status_code=503)

    provided = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    results = []
    for c in body.get("companies", []):
        name, adapter, slug = c.get("name", ""), c.get("ats", ""), c.get("slug", "")
        ok, message = _try_add_board(name, adapter.lower(), slug)
        results.append({"ok": ok, "message": message, "name": name})
    return JSONResponse({"results": results})


@app.post("/boards/{board_id}/toggle")
def boards_toggle(board_id: int):
    with db.connect() as conn:
        db.toggle_board(conn, board_id)
    return RedirectResponse("/boards", status_code=303)


@app.post("/boards/{board_id}/delete")
def boards_delete(board_id: int):
    with db.connect() as conn:
        db.delete_board(conn, board_id)
    return RedirectResponse("/boards", status_code=303)


@app.post("/searches/{search_id}/toggle")
def searches_toggle(search_id: int):
    with db.connect() as conn:
        db.toggle_search(conn, search_id)
    return RedirectResponse("/boards", status_code=303)


# -------------------------------------------------------------- discovered

@app.get("/discovered", response_class=HTMLResponse)
def discovered_page(request: Request):
    with db.connect() as conn:
        db.init_schema(conn)
        rows = db.list_discovered(conn)
    return templates.TemplateResponse(request, "discovered.html", {
        "active": "discovered", "rows": rows, **flash_ctx(request),
    })


@app.post("/discovered/{company}/dismiss")
def discovered_dismiss(company: str):
    with db.connect() as conn:
        db.delete_discovered(conn, company)
    return RedirectResponse("/discovered", status_code=303)


# ------------------------------------------------------------- scheduler

def _next_run_at(now: datetime) -> datetime:
    candidate = now.replace(hour=SCHEDULE_HOUR_UTC, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() not in SCHEDULE_WEEKDAYS:
        candidate += timedelta(days=1)
    return candidate


async def _scheduler_loop():
    while True:
        now = datetime.now(timezone.utc)
        target = _next_run_at(now)
        sleep_seconds = (target - now).total_seconds()
        log.info("scheduler: next agent.py run at %s UTC (sleeping %.0fs)", target, sleep_seconds)
        await asyncio.sleep(sleep_seconds)
        log.info("scheduler: starting agent.py")
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "agent.py")],
                capture_output=True, text=True, env=os.environ.copy(),
            )
            log.info("scheduler: agent.py exited %s\n%s", result.returncode,
                      result.stdout[-2000:] + result.stderr[-2000:])
        except Exception:
            log.exception("scheduler: agent.py run failed to launch")


@app.on_event("startup")
async def start_scheduler():
    asyncio.create_task(_scheduler_loop())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
