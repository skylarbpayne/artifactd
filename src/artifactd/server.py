from __future__ import annotations

import html
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse

from .security import sign_artifact_cookie, verify_artifact_cookie, verify_password
from .store import Artifact, ArtifactStore

DEFAULT_HOME = Path(os.environ.get("ARTIFACTD_HOME", "~/.hermes/artifacts")).expanduser()
DEFAULT_COOKIE_SECRET = os.environ.get("ARTIFACTD_COOKIE_SECRET", "dev-only-change-me")


def create_app(home: Path = DEFAULT_HOME, *, cookie_secret: Optional[str] = None) -> FastAPI:
    store = ArtifactStore(Path(home))
    secret = cookie_secret or DEFAULT_COOKIE_SECRET
    app = FastAPI(title="artifactd")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        items = []
        for artifact in store.list():
            lock = " 🔒" if artifact.has_password else ""
            items.append(f'<li><a href="/{html.escape(artifact.slug)}">{html.escape(artifact.title)}</a>{lock}</li>')
        return "<h1>artifactd</h1><ul>" + "".join(items) + "</ul>"

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

    @app.get("/{slug}")
    async def artifact_index(slug: str, request: Request) -> Response:
        return _serve_artifact(store, slug, "", request, secret)

    @app.get("/{slug}/{relative_path:path}")
    async def artifact_file(slug: str, relative_path: str, request: Request) -> Response:
        return _serve_artifact(store, slug, relative_path, request, secret)

    return app


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
