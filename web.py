#!/usr/bin/env python3
"""
Local Configuration + Boards UI. No authentication — binds to 127.0.0.1
only, meant for your own machine. Add auth (see SPEC-v2.md section 4)
before this is ever reachable from anywhere else.

    python web.py
    -> http://127.0.0.1:8000/
"""
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import agent
import db
import sources

ROOT = Path(__file__).parent
app = FastAPI()
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["css"] = agent.CSS

NAV = (
    '<div class="nav">'
    '<a href="/" aria-current="page">Board</a>'
    '<a href="/runs">Runs</a>'
    '<a href="/config">Configuration</a>'
    '<a href="/boards">Boards</a>'
    "</div>"
)


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
        "companies": companies, "searches": searches, **flash_ctx(request),
    })


@app.post("/boards/add")
def boards_add(name: str = Form(...), adapter: str = Form(...), slug: str = Form(...)):
    """
    Test-fetches with the real, hardcoded ATS adapter before saving. This is
    NOT the phase-4 arbitrary-URL board tester — adapter and URL template are
    both fixed and already trusted (sources.ATS_FETCHERS), so there's no SSRF
    surface here, just "does this slug actually resolve."
    """
    name, slug = name.strip(), slug.strip()
    fetcher = sources.ATS_FETCHERS.get(adapter)
    if not fetcher:
        return redirect_with_flash("/boards", f"Unknown ATS '{adapter}'.", error=True)
    try:
        found = fetcher(slug, name)
    except Exception as exc:
        return redirect_with_flash(
            "/boards", f"Could not fetch {name} ({adapter}/{slug}): {exc}", error=True)
    if not found:
        return redirect_with_flash(
            "/boards",
            f"{name} ({adapter}/{slug}) is reachable but returned 0 postings — check the slug.",
            error=True)

    with db.connect() as conn:
        db.init_schema(conn)
        db.add_board(conn, name, adapter, slug)
    example = found[0].get("title", "")
    return redirect_with_flash(
        "/boards", f'Added {name} — found {len(found)} posting(s), e.g. "{example}".')


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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
