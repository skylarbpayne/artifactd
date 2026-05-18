from __future__ import annotations

import html
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse

from .interactive import register_interactive_routes
from .security import sign_artifact_cookie, verify_artifact_cookie, verify_password
from .store import Artifact, ArtifactStore

DEFAULT_HOME = Path(os.environ.get("ARTIFACTD_HOME", "~/.hermes/artifacts")).expanduser()
DEFAULT_COOKIE_SECRET = os.environ.get("ARTIFACTD_COOKIE_SECRET", "dev-only-change-me")


def create_app(home: Path = DEFAULT_HOME, *, cookie_secret: Optional[str] = None) -> FastAPI:
    store = ArtifactStore(Path(home))
    secret = cookie_secret or DEFAULT_COOKIE_SECRET
    app = FastAPI(title="artifactd")

    @app.get("/", response_class=HTMLResponse)
    def index(q: str = "") -> str:
        return _index_page(list(store.search(q)), query=q)

    @app.post("/{slug}/login")
    async def login(slug: str, password: str = Form(...)) -> Response:
        artifact = store.get(slug)
        if not artifact:
            raise HTTPException(status_code=404, detail="artifact not found")
        if not artifact.password_hash or not verify_password(password, artifact.password_hash):
            return _password_page(artifact, status_code=401, message="Wrong password")
        response = RedirectResponse(url=f"/{artifact.slug}", status_code=303)
        response.set_cookie(
            _cookie_name(artifact.slug),
            sign_artifact_cookie(artifact.slug, secret),
            httponly=True,
            samesite="lax",
            secure=False,
            path=f"/{artifact.slug}",
        )
        return response

    register_interactive_routes(app, store, secret)

    @app.get("/{slug}")
    async def artifact_index(slug: str, request: Request) -> Response:
        return _serve_artifact(store, slug, "", request, secret)

    @app.get("/{slug}/{relative_path:path}")
    async def artifact_file(slug: str, relative_path: str, request: Request) -> Response:
        return _serve_artifact(store, slug, relative_path, request, secret)

    return app


def _index_page(artifacts: list[Artifact], *, query: str = "") -> str:
    escaped_query = html.escape(query.strip(), quote=True)
    cards = []
    for artifact in artifacts:
        slug = html.escape(artifact.slug)
        title = html.escape(artifact.title)
        description = html.escape(artifact.description or "No description yet.")
        visibility = "Protected" if artifact.has_password else "Public"
        lock = "🔒" if artifact.has_password else "↗"
        cards.append(
            f"""
            <article class="card">
              <div class="card-top">
                <p class="eyebrow">{html.escape(visibility)}</p>
                <span aria-hidden="true">{lock}</span>
              </div>
              <h2><a href="/{slug}">{title}</a></h2>
              <p>{description}</p>
              <code>/{slug}</code>
            </article>
            """
        )
    if not cards:
        empty = "No artifacts match that search." if escaped_query else "No artifacts deployed yet."
        cards.append(f'<p class="empty">{html.escape(empty)}</p>')
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>artifactd</title>
        <style>
          :root {{ color-scheme: dark; --bg: #0b0f19; --panel: rgba(255,255,255,.07); --line: rgba(255,255,255,.13); --text: #eef2ff; --muted: #9aa7bd; --accent: #8b5cf6; }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; min-height: 100vh; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); background: radial-gradient(circle at top left, rgba(139,92,246,.35), transparent 32rem), var(--bg); }}
          main {{ width: min(1080px, calc(100% - 32px)); margin: 0 auto; padding: 64px 0; }}
          header {{ display: grid; gap: 18px; margin-bottom: 32px; }}
          h1 {{ margin: 0; font-size: clamp(2.4rem, 7vw, 5rem); letter-spacing: -.06em; }}
          .lede {{ margin: 0; max-width: 42rem; color: var(--muted); font-size: 1.08rem; line-height: 1.6; }}
          form {{ display: flex; gap: 10px; max-width: 44rem; }}
          input {{ flex: 1; min-width: 0; border: 1px solid var(--line); border-radius: 999px; padding: 14px 18px; background: rgba(255,255,255,.08); color: var(--text); font: inherit; }}
          button {{ border: 0; border-radius: 999px; padding: 14px 18px; background: var(--accent); color: white; font: inherit; font-weight: 700; cursor: pointer; }}
          .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; }}
          .card {{ border: 1px solid var(--line); border-radius: 24px; padding: 22px; background: var(--panel); box-shadow: 0 20px 70px rgba(0,0,0,.22); }}
          .card-top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
          .eyebrow {{ margin: 0; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; font-size: .72rem; font-weight: 800; }}
          h2 {{ margin: 18px 0 10px; font-size: 1.35rem; letter-spacing: -.03em; }}
          a {{ color: inherit; text-decoration: none; }}
          a:hover {{ text-decoration: underline; }}
          .card p:not(.eyebrow) {{ min-height: 3em; color: var(--muted); line-height: 1.5; }}
          code {{ color: #c4b5fd; }}
          .empty {{ border: 1px dashed var(--line); border-radius: 20px; padding: 28px; color: var(--muted); }}
        </style>
      </head>
      <body>
        <main>
          <header>
            <p class="eyebrow">artifactd</p>
            <h1>Artifact home</h1>
            <p class="lede">A local-first index of Palmer artifacts. Search by title, slug, or description; protected artifacts still require their own password when opened.</p>
            <form method="get" action="/" role="search">
              <input type="search" name="q" value="{escaped_query}" placeholder="Search artifacts" aria-label="Search artifacts">
              <button type="submit">Search artifacts</button>
            </form>
          </header>
          <section class="grid" aria-label="Artifacts">
            {''.join(cards)}
          </section>
        </main>
      </body>
    </html>
    """


def _serve_artifact(store: ArtifactStore, slug: str, relative_path: str, request: Request, secret: str) -> Response:
    artifact = store.get(slug)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.password_hash:
        cookie = request.cookies.get(_cookie_name(artifact.slug))
        if not verify_artifact_cookie(artifact.slug, cookie, secret):
            return _password_page(artifact, status_code=401)
    try:
        file_path = store.resolve_file(artifact, relative_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(file_path)


def _password_page(artifact: Artifact, *, status_code: int = 401, message: str = "Password required") -> HTMLResponse:
    escaped_slug = html.escape(artifact.slug)
    escaped_message = html.escape(message)
    body = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Password required · {escaped_slug}</title>
        <style>
          body {{ font-family: system-ui, sans-serif; max-width: 34rem; margin: 12vh auto; padding: 0 1rem; }}
          input, button {{ font: inherit; padding: .65rem .8rem; }}
          form {{ display: flex; gap: .5rem; }}
          input {{ flex: 1; }}
        </style>
      </head>
      <body>
        <h1>{escaped_message}</h1>
        <p>This artifact is protected.</p>
        <form method="post" action="/{escaped_slug}/login">
          <input type="password" name="password" autocomplete="current-password" autofocus>
          <button type="submit">Unlock</button>
        </form>
      </body>
    </html>
    """
    return HTMLResponse(body, status_code=status_code)


def _cookie_name(slug: str) -> str:
    return f"artifactd_auth_{slug.replace('-', '_')}"


app = create_app()
