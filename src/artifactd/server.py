from __future__ import annotations

import html
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .actions import CAPABILITY_REGISTRY, KanbanExecutor, register_action_routes
from .interactive import register_interactive_routes
from .security import sign_artifact_cookie, sign_csrf_token, verify_artifact_cookie, verify_csrf_token, verify_password
from .store import Artifact, ArtifactStore

DEFAULT_HOME = Path(os.environ.get("ARTIFACTD_HOME", "~/.hermes/artifacts")).expanduser()
DEFAULT_COOKIE_SECRET = os.environ.get("ARTIFACTD_COOKIE_SECRET", "dev-only-change-me")


def create_app(
    home: Path = DEFAULT_HOME,
    *,
    cookie_secret: Optional[str] = None,
    kanban_executor: Optional[KanbanExecutor] = None,
    profile: Optional[str] = None,
    public_base_url: Optional[str] = None,
) -> FastAPI:
    store = ArtifactStore(Path(home))
    secret = cookie_secret or DEFAULT_COOKIE_SECRET
    executor = kanban_executor or KanbanExecutor(profile=profile)
    workspace_profile = profile or getattr(executor, "profile", "default")
    public_base_url = (public_base_url or os.environ.get("ARTIFACTD_PUBLIC_BASE_URL") or "").rstrip("/") or None
    app = FastAPI(title="artifactd")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = "", bucket: str = "active", tag: list[str] = Query(default=[])) -> Response:
        session_cookie = _workspace_session_cookie(request, secret)
        if store.workspace_password_configured() and not session_cookie:
            return _workspace_password_page(status_code=401)
        csrf_token = _workspace_csrf_token(session_cookie, secret)
        selected_tags = _selected_tags(tag)
        return HTMLResponse(
            _index_page(
                _workspace_bucket(store, query=q, bucket=bucket, tags=selected_tags),
                query=q,
                heading="Workspace Home",
                archive=False,
                bucket=bucket,
                counts=_workspace_counts(store),
                tag_facets=store.tag_facets(bucket=_normalized_bucket(bucket)),
                selected_tags=selected_tags,
                csrf_token=csrf_token,
                profile=workspace_profile,
            )
        )

    @app.get("/archive", response_class=HTMLResponse)
    def archive(request: Request, q: str = "", tag: list[str] = Query(default=[])) -> Response:
        session_cookie = _workspace_session_cookie(request, secret)
        if store.workspace_password_configured() and not session_cookie:
            return _workspace_password_page(status_code=401)
        selected_tags = _selected_tags(tag)
        return HTMLResponse(
            _index_page(
                _workspace_bucket(store, query=q, bucket="archived", tags=selected_tags),
                query=q,
                heading="Archived things",
                archive=True,
                bucket="archived",
                counts=_workspace_counts(store),
                tag_facets=store.tag_facets(bucket="archived"),
                selected_tags=selected_tags,
                csrf_token=_workspace_csrf_token(session_cookie, secret),
                profile=workspace_profile,
            )
        )

    @app.post("/_workspace/login")
    async def workspace_login(password: str = Form(...)) -> Response:
        if not store.verify_workspace_password(password):
            return _workspace_password_page(status_code=401, message="Wrong password")
        return _workspace_login_response(secret, "/")

    @app.get("/_workspace/things")
    def workspace_things(request: Request, q: str = "", tag: list[str] = Query(default=[])) -> dict[str, object]:
        session_cookie = _workspace_session_cookie(request, secret)
        if store.workspace_password_configured() and not session_cookie:
            raise HTTPException(status_code=401, detail="workspace password required")
        selected_tags = _selected_tags(tag)
        return {
            "buckets": {
                "active": [_thing_payload(artifact) for artifact in _workspace_bucket(store, query=q, bucket="active", tags=selected_tags)],
                "pinned": [_thing_payload(artifact) for artifact in _workspace_bucket(store, query=q, bucket="pinned", tags=selected_tags)],
                "recent": [_thing_payload(artifact) for artifact in _workspace_bucket(store, query=q, bucket="recent", tags=selected_tags)],
                "requires_action": [_thing_payload(artifact) for artifact in _workspace_bucket(store, query=q, bucket="requires-action", tags=selected_tags)],
                "archived": [_thing_payload(artifact) for artifact in _workspace_bucket(store, query=q, bucket="archived", tags=selected_tags)],
            },
            "counts": _workspace_counts(store),
            "tag_facets": store.tag_facets(bucket="active"),
            "selected_tags": selected_tags,
        }

    @app.get("/_workspace/home")
    def workspace_home(request: Request, q: str = "", tag: list[str] = Query(default=[])) -> dict[str, object]:
        session_cookie = _workspace_session_cookie(request, secret)
        if store.workspace_password_configured() and not session_cookie:
            raise HTTPException(status_code=401, detail="workspace password required")
        csrf_token = _workspace_csrf_token(session_cookie, secret)
        return _workspace_home_payload(store, profile=workspace_profile, executor=executor, csrf_token=csrf_token, query=q, tags=_selected_tags(tag))

    @app.post("/_workspace/things/{slug}/pin")
    async def workspace_pin(slug: str, request: Request, pinned: str = Form("true"), csrf_token: str = Form("")) -> Response:
        _require_workspace_mutation_session(store, request, secret, csrf_token)
        artifact = store.update_metadata(slug, pinned=_truthy(pinned))
        store.record_action_audit(
            slug=artifact.slug,
            capability="workspace.pin",
            actor="workspace-session",
            payload_hash="-",
            status="ok",
            result_summary=f"pinned={artifact.pinned}",
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/_workspace/things/{slug}/archive")
    async def workspace_archive(slug: str, request: Request, csrf_token: str = Form("")) -> Response:
        _require_workspace_mutation_session(store, request, secret, csrf_token)
        artifact = store.archive(slug, reason="Archived from Workspace Home", force=True)
        store.record_action_audit(
            slug=artifact.slug,
            capability="workspace.archive",
            actor="workspace-session",
            payload_hash="-",
            status="ok",
            result_summary=f"status={artifact.status}",
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/_workspace/things/{slug}/share")
    async def workspace_share(
        slug: str,
        request: Request,
        csrf_token: str = Form(""),
        expires_in: str = Form("604800"),
        expires_at: str = Form(""),
    ) -> Response:
        _require_workspace_mutation_session(store, request, secret, csrf_token)
        now = int(time.time())
        try:
            ttl_seconds, expiry_kind = _share_expiry_from_form(expires_in, expires_at, now=now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        token = store.create_share_override(slug, ttl_seconds=ttl_seconds, now=now)
        artifact = store.get(slug)
        if not artifact:
            raise HTTPException(status_code=404, detail="artifact not found")
        store.record_action_audit(
            slug=artifact.slug,
            capability="workspace.share",
            actor="workspace-session",
            payload_hash="-",
            status="ok",
            result_summary=f"share override created; expires_at={artifact.share_token_expires_at}",
        )
        return _share_page(artifact, token, request, public_base_url=public_base_url, expiry_kind=expiry_kind)

    @app.post("/_workspace/things/{slug}/requires-action")
    async def workspace_requires_action(slug: str, request: Request, requires_action: str = Form("true"), csrf_token: str = Form("")) -> Response:
        _require_workspace_mutation_session(store, request, secret, csrf_token)
        artifact = store.set_requires_action(slug, _truthy(requires_action))
        store.record_action_audit(
            slug=artifact.slug,
            capability="workspace.requires_action",
            actor="workspace-session",
            payload_hash="-",
            status="ok",
            result_summary=f"requires_action={artifact.requires_action}",
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/{slug}/login")
    async def login(slug: str, password: str = Form(...)) -> Response:
        artifact = store.get(slug)
        if not artifact:
            raise HTTPException(status_code=404, detail="artifact not found")
        if store.workspace_password_configured() and store.verify_workspace_password(password):
            return _workspace_login_response(secret, f"/{artifact.slug}")
        return _password_page(artifact, status_code=401, message="Wrong password")

    register_action_routes(app, store, secret, kanban_executor=executor)
    register_interactive_routes(app, store, secret)

    @app.get("/{slug}")
    async def artifact_index(slug: str, request: Request) -> Response:
        return _serve_artifact(store, slug, "", request, secret)

    @app.get("/{slug}/{relative_path:path}")
    async def artifact_file(slug: str, relative_path: str, request: Request) -> Response:
        return _serve_artifact(store, slug, relative_path, request, secret)

    return app


def _index_page(
    artifacts: list[Artifact],
    *,
    query: str = "",
    heading: str = "Artifact home",
    archive: bool = False,
    bucket: str = "active",
    counts: Optional[dict[str, int]] = None,
    tag_facets: Optional[dict[str, int]] = None,
    selected_tags: Optional[list[str]] = None,
    csrf_token: str = "",
    profile: str = "default",
) -> str:
    escaped_query = html.escape(query.strip(), quote=True)
    escaped_heading = html.escape(heading)
    escaped_bucket = html.escape(bucket, quote=True)
    escaped_profile = html.escape(profile)
    counts = counts or {}
    tag_facets = tag_facets or {}
    selected_tags = selected_tags or []
    cards = []
    for artifact in artifacts:
        slug = html.escape(artifact.slug)
        title = html.escape(artifact.title)
        description = html.escape(artifact.description or "No description yet.")
        visibility = "Protected" if artifact.has_password or artifact.uses_profile_auth else "Public"
        if artifact.status == "archived":
            visibility = f"Archived · {visibility}"
        if artifact.pinned:
            visibility = f"Pinned · {visibility}"
        lock = "🔒" if artifact.has_password or artifact.uses_profile_auth else "↗"
        meta = []
        if artifact.requires_action:
            meta.append("Requires action")
        if artifact.expires_at is not None:
            meta.append(f"expires_at={artifact.expires_at}")
        if artifact.capabilities:
            meta.append(f"Actions: {len(artifact.capabilities)}")
        if artifact.archive_reason:
            meta.append("reason=" + artifact.archive_reason)
        meta_html = f"<p class=\"meta\">{html.escape(' · '.join(meta))}</p>" if meta else ""
        tag_html = _tag_chip_html(artifact.tags)
        workspace_actions = _workspace_action_forms(artifact, csrf_token)
        cards.append(
            f"""
            <article class="card">
              <div class="card-top">
                <p class="eyebrow">{html.escape(visibility)}</p>
                <span aria-hidden="true">{lock}</span>
              </div>
              <h2><a href="/{slug}">{title}</a></h2>
              <p>{description}</p>
              {meta_html}
              {tag_html}
              <p class="actions"><a href="/{slug}">Open</a> · <a href="/{slug}/_actions">Update</a></p>
              {workspace_actions}
              <code>/{slug}</code>
            </article>
            """
        )

    if not cards:
        if escaped_query:
            empty = "No archived artifacts match that search." if archive else "No artifacts match that search."
        else:
            empty = "No archived artifacts." if archive else "No artifacts deployed yet."
        cards.append(f'<p class="empty">{html.escape(empty)}</p>')
    nav_href = "/" if archive else "/archive"
    nav_label = "Active artifacts" if archive else "Archive"
    lede = (
        "Archived artifacts are hidden from the home page but remain recoverable until pruned."
        if archive
        else "One workspace for generated Things. Each card has Open, Share link, Update, Pin, and Archive controls; profile-auth protected Things share one workspace session by default."
    )
    bucket_links = ""
    if not archive:
        bucket_items = [
            ("active", "Active"),
            ("pinned", "Pinned"),
            ("recent", "Recent"),
            ("requires-action", "Requires action"),
        ]
        bucket_links = '<div class="buckets">' + "".join(
            f'<a class="{html.escape("selected" if name == bucket else "")}" href="/?bucket={html.escape(name, quote=True)}">{html.escape(label)} <span>{counts.get(name, 0)}</span></a>'
            for name, label in bucket_items
        ) + "</div>"
    tag_filters = _tag_filter_html(
        tag_facets,
        selected_tags=selected_tags,
        query=query,
        bucket=bucket,
        archive=archive,
    )
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
          nav a {{ display: inline-flex; width: fit-content; border: 1px solid var(--line); border-radius: 999px; padding: 10px 14px; color: var(--text); text-decoration: none; }}
          h1 {{ margin: 0; font-size: clamp(2.4rem, 7vw, 5rem); letter-spacing: -.06em; }}
          .lede, .bridge {{ margin: 0; max-width: 42rem; color: var(--muted); font-size: 1.08rem; line-height: 1.6; }}
          .bridge {{ color: #c4b5fd; font-size: .92rem; }}
          form.search {{ display: flex; gap: 10px; max-width: 44rem; }}
          input, select {{ flex: 1; min-width: 0; border: 1px solid var(--line); border-radius: 999px; padding: 14px 18px; background: rgba(255,255,255,.08); color: var(--text); font: inherit; }}
          select option {{ color: #0b0f19; }}
          button {{ border: 0; border-radius: 999px; padding: 14px 18px; background: var(--accent); color: white; font: inherit; font-weight: 700; cursor: pointer; }}
          .buckets {{ display: flex; flex-wrap: wrap; gap: 10px; }}
          .buckets a {{ border: 1px solid var(--line); border-radius: 999px; padding: 10px 14px; color: var(--text); text-decoration: none; }}
          .buckets a.selected {{ background: rgba(139,92,246,.28); border-color: rgba(196,181,253,.6); }}
          .buckets span {{ color: #c4b5fd; font-weight: 800; }}
          .tag-filters, .tags {{ display: flex; flex-wrap: wrap; gap: 8px; }}
          .tag-chip {{ display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--line); border-radius: 999px; padding: 5px 9px; color: #dbeafe; background: rgba(59,130,246,.12); font-size: .82rem; text-decoration: none; }}
          .tag-chip.selected {{ border-color: rgba(196,181,253,.75); background: rgba(139,92,246,.35); }}
          .tag-chip span {{ color: #c4b5fd; font-weight: 800; }}
          .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; }}
          .card {{ border: 1px solid var(--line); border-radius: 24px; padding: 22px; background: var(--panel); box-shadow: 0 20px 70px rgba(0,0,0,.22); overflow: hidden; overflow-wrap: anywhere; }}
          .card-top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
          .eyebrow {{ margin: 0; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; font-size: .72rem; font-weight: 800; }}
          h2 {{ margin: 18px 0 10px; font-size: 1.35rem; letter-spacing: -.03em; }}
          a {{ color: inherit; text-decoration: none; }}
          a:hover {{ text-decoration: underline; }}
          .card p:not(.eyebrow) {{ min-height: 3em; color: var(--muted); line-height: 1.5; }}
          .card p.meta {{ min-height: 0; font-size: .85rem; }}
          .workspace-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; align-items: center; }}
          .workspace-actions form {{ margin: 0; }}
          .workspace-actions .share-form {{ display: flex; flex: 1 1 100%; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px; border: 1px solid var(--line); border-radius: 18px; background: rgba(139,92,246,.12); }}
          .workspace-actions .share-form label {{ display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: .82rem; font-weight: 800; }}
          .workspace-actions .share-form select, .workspace-actions .share-form input[type="datetime-local"] {{ flex: 0 1 145px; padding: 8px 10px; font-size: .88rem; }}
          .workspace-actions .custom-share-expiry {{ flex: 1 1 100%; color: var(--muted); font-size: .82rem; }}
          .workspace-actions .custom-share-expiry summary {{ cursor: pointer; width: fit-content; }}
          .workspace-actions button {{ padding: 8px 11px; background: rgba(255,255,255,.12); border: 1px solid var(--line); font-size: .88rem; }}
          .workspace-actions button.share {{ background: var(--accent); border-color: rgba(196,181,253,.75); }}
          .workspace-actions button.danger {{ background: rgba(239,68,68,.18); }}
          code {{ color: #c4b5fd; }}
          .empty {{ border: 1px dashed var(--line); border-radius: 20px; padding: 28px; color: var(--muted); }}
        </style>
      </head>
      <body>
        <main>
          <header>
            <p class="eyebrow">artifactd</p>
            <h1>{escaped_heading}</h1>
            <p class="bridge">Hermes profile bridge · profile={escaped_profile} · server-side actions route through scoped capabilities and audit.</p>
            <p class="lede">{html.escape(lede)}</p>
            <nav><a href="{nav_href}">{nav_label}</a></nav>
            {bucket_links}
            {tag_filters}
            <form class="search" method="get" action="{'/archive' if archive else '/'}" role="search">
              <input type="hidden" name="bucket" value="{escaped_bucket}">
              {''.join(f'<input type="hidden" name="tag" value="{html.escape(tag, quote=True)}">' for tag in selected_tags)}
              <input type="search" name="q" value="{escaped_query}" placeholder="Search things" aria-label="Search things">
              <button type="submit">Search things</button>
            </form>
          </header>
          <section class="grid" aria-label="Artifacts">
            {''.join(cards)}
          </section>
        </main>
      </body>
    </html>
    """


def _workspace_action_forms(artifact: Artifact, csrf_token: str) -> str:
    if not csrf_token or artifact.is_archived:
        return ""
    slug = html.escape(artifact.slug, quote=True)
    token = html.escape(csrf_token, quote=True)
    pin_value = "false" if artifact.pinned else "true"
    pin_label = "Unpin" if artifact.pinned else "Pin"
    action_value = "false" if artifact.requires_action else "true"
    action_label = "Clear action" if artifact.requires_action else "Requires action"
    return f"""
      <div class="workspace-actions" aria-label="Workspace actions">
        <form class="share-form" method="post" action="/_workspace/things/{slug}/share">
          <input type="hidden" name="csrf_token" value="{token}">
          {_share_expiry_controls()}
          <button class="share" type="submit">Share link</button>
        </form>
        <form method="post" action="/_workspace/things/{slug}/pin"><input type="hidden" name="csrf_token" value="{token}"><input type="hidden" name="pinned" value="{pin_value}"><button type="submit">{pin_label}</button></form>
        <form method="post" action="/_workspace/things/{slug}/requires-action"><input type="hidden" name="csrf_token" value="{token}"><input type="hidden" name="requires_action" value="{action_value}"><button type="submit">{action_label}</button></form>
        <form method="post" action="/_workspace/things/{slug}/archive"><input type="hidden" name="csrf_token" value="{token}"><button class="danger" type="submit">Archive</button></form>
      </div>
    """


def _tag_chip_html(tags: tuple[str, ...]) -> str:
    if not tags:
        return ""
    chips = "".join(f'<span class="tag-chip">{html.escape(tag)}</span>' for tag in tags)
    return f'<div class="tags" aria-label="Thing tags">{chips}</div>'


def _tag_filter_html(tag_facets: dict[str, int], *, selected_tags: list[str], query: str, bucket: str, archive: bool) -> str:
    if not tag_facets:
        return ""
    selected = set(selected_tags)
    chips = []
    for tag, count in tag_facets.items():
        next_tags = [item for item in selected_tags if item != tag] if tag in selected else [*selected_tags, tag]
        css = "tag-chip selected" if tag in selected else "tag-chip"
        chips.append(
            f'<a class="{css}" data-tag-filter href="{html.escape(_tag_filter_href(query=query, bucket=bucket, tags=next_tags, archive=archive), quote=True)}">'
            f'{html.escape(tag)} <span>{count}</span></a>'
        )
    return '<div class="tag-filters" aria-label="Filter by tag">' + "".join(chips) + "</div>"


def _tag_filter_href(*, query: str, bucket: str, tags: list[str], archive: bool) -> str:
    path = "/archive" if archive else "/"
    params: list[tuple[str, str]] = []
    if query.strip():
        params.append(("q", query.strip()))
    if not archive:
        params.append(("bucket", _normalized_bucket(bucket)))
    for tag in tags:
        params.append(("tag", tag))
    return path + ("?" + urlencode(params) if params else "")


def _selected_tags(values: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in str(value).split(","):
            tag = " ".join(part.strip().lower().split())
            if not tag or tag in seen:
                continue
            seen.add(tag)
            selected.append(tag)
    return selected


def _normalized_bucket(bucket: str) -> str:
    normalized = (bucket or "active").lower()
    return normalized if normalized in {"active", "pinned", "recent", "requires-action", "archived"} else "active"


def _workspace_bucket(store: ArtifactStore, *, query: str = "", bucket: str = "active", tags: Optional[list[str]] = None) -> list[Artifact]:
    normalized = _normalized_bucket(bucket)
    things = store.list_workspace_things(bucket=normalized, tags=tags or [])
    needle = query.strip().lower()
    if not needle:
        return things
    return [thing for thing in things if needle in thing.slug.lower() or needle in thing.title.lower() or needle in (thing.description or "").lower()]


def _workspace_counts(store: ArtifactStore) -> dict[str, int]:
    return {
        "active": len(store.list_workspace_things(bucket="active")),
        "pinned": len(store.list_workspace_things(bucket="pinned")),
        "recent": len(store.list_workspace_things(bucket="recent")),
        "requires-action": len(store.list_workspace_things(bucket="requires-action")),
        "archived": len(store.list_workspace_things(bucket="archived")),
    }


def _thing_payload(artifact: Artifact) -> dict[str, object]:
    return {
        "slug": artifact.slug,
        "title": artifact.title,
        "description": artifact.description,
        "status": artifact.status,
        "auth_mode": artifact.auth_mode,
        "protected": artifact.has_password or artifact.uses_profile_auth,
        "pinned": artifact.pinned,
        "requires_action": artifact.requires_action,
        "capabilities": list(artifact.capabilities),
        "tags": list(artifact.tags),
        "updated_at": artifact.updated_at,
        "path": f"/{artifact.slug}",
    }


def _workspace_home_payload(store: ArtifactStore, *, profile: str, executor: KanbanExecutor, csrf_token: str, query: str = "", tags: Optional[list[str]] = None) -> dict[str, object]:
    bucket_names = ["active", "pinned", "recent", "requires-action", "archived"]
    selected_tags = tags or []
    return {
        "kind": "HermesWorkspaceHome",
        "title": "Workspace Home",
        "profile": profile,
        "language": {"home": "Home", "thing": "Thing", "things": "Things"},
        "counts": _workspace_counts(store),
        "tag_facets": store.tag_facets(bucket="active"),
        "selected_tags": selected_tags,
        "buckets": {
            _payload_bucket_key(bucket): [
                _workspace_home_thing_payload(artifact, profile=profile, executor=executor, csrf_token=csrf_token)
                for artifact in _workspace_bucket(store, query=query, bucket=bucket, tags=selected_tags)
            ]
            for bucket in bucket_names
        },
    }


def _payload_bucket_key(bucket: str) -> str:
    return "requires_action" if bucket == "requires-action" else bucket


def _workspace_home_thing_payload(artifact: Artifact, *, profile: str, executor: KanbanExecutor, csrf_token: str) -> dict[str, object]:
    payload = _thing_payload(artifact)
    payload.update(
        {
            "open_url": f"/{artifact.slug}",
            "actions_url": f"/{artifact.slug}/_actions",
            "workspace_actions": _workspace_action_payloads(artifact, csrf_token),
            "capability_bridge": {
                "provider": "hermes-profile",
                "profile": profile,
                "audit_actor": str(getattr(executor, "actor", f"hermes-profile:{profile}")),
            },
            "capabilities": _capabilities_payload(artifact, profile=profile),
        }
    )
    return payload


def _workspace_action_payloads(artifact: Artifact, csrf_token: str) -> dict[str, dict[str, object]]:
    return {
        "share": {"method": "POST", "url": f"/_workspace/things/{artifact.slug}/share", "requires_csrf": True, "csrf_token": csrf_token, "approval_required": False},
        "pin": {"method": "POST", "url": f"/_workspace/things/{artifact.slug}/pin", "requires_csrf": True, "csrf_token": csrf_token, "approval_required": False},
        "requires_action": {"method": "POST", "url": f"/_workspace/things/{artifact.slug}/requires-action", "requires_csrf": True, "csrf_token": csrf_token, "approval_required": False},
        "archive": {"method": "POST", "url": f"/_workspace/things/{artifact.slug}/archive", "requires_csrf": True, "csrf_token": csrf_token, "approval_required": False},
    }


def _capabilities_payload(artifact: Artifact, *, profile: str) -> dict[str, dict[str, object]]:
    capabilities: dict[str, dict[str, object]] = {}
    for name in artifact.capabilities:
        capability = CAPABILITY_REGISTRY.get(name)
        if not capability:
            continue
        item: dict[str, object] = {
            "name": capability.name,
            "description": capability.description,
            "schema": capability.schema,
            "provider": capability.provider,
            "executes_via": capability.executes_via,
            "approval_required": capability.approval_required,
        }
        if capability.provider == "hermes-profile":
            item["profile"] = profile
        capabilities[name] = item
    return capabilities


def _workspace_session_cookie(request: Request, secret: str) -> Optional[str]:
    cookie = request.cookies.get(_workspace_cookie_name())
    if verify_artifact_cookie("__workspace__", cookie, secret):
        return cookie
    return None


def _workspace_login_response(secret: str, redirect_url: str) -> RedirectResponse:
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        _workspace_cookie_name(),
        sign_artifact_cookie("__workspace__", secret),
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


def _workspace_csrf_token(session_cookie: Optional[str], secret: str) -> str:
    if not session_cookie:
        return ""
    return sign_csrf_token("__workspace__", session_cookie, secret)


def _require_workspace_mutation_session(store: ArtifactStore, request: Request, secret: str, csrf_token: str) -> str:
    if not store.workspace_password_configured():
        raise HTTPException(status_code=403, detail="workspace password must be configured before workspace actions can run")
    session_cookie = _workspace_session_cookie(request, secret)
    if not session_cookie:
        raise HTTPException(status_code=401, detail="workspace password required")
    if not verify_csrf_token("__workspace__", csrf_token, session_cookie, secret):
        raise HTTPException(status_code=403, detail="workspace CSRF token is missing or invalid")
    return session_cookie


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _share_expiry_options_html() -> str:
    return """
              <option value="3600">1 hour</option>
              <option value="86400">1 day</option>
              <option value="604800" selected>7 days</option>
              <option value="2592000">30 days</option>
              <option value="custom">Custom</option>"""


def _share_expiry_controls(*, select_style: str = "", datetime_style: str = "") -> str:
    escaped_select_style = html.escape(select_style, quote=True)
    escaped_datetime_style = html.escape(datetime_style, quote=True)
    select_style_attr = f' style="{escaped_select_style}"' if escaped_select_style else ""
    datetime_style_attr = f' style="{escaped_datetime_style}"' if escaped_datetime_style else ""
    return f"""
          <label>Share duration
            <select name="expires_in" aria-label="Share duration"{select_style_attr}>{_share_expiry_options_html()}
            </select>
          </label>
          <details class="custom-share-expiry">
            <summary>Custom expiry</summary>
            <input type="datetime-local" name="expires_at" aria-label="Custom share expiry" title="Custom share expiry in UTC"{datetime_style_attr}>
          </details>"""


_SHARE_EXPIRY_PRESETS = {
    "3600": (60 * 60, "1 hour"),
    "86400": (24 * 60 * 60, "1 day"),
    "604800": (7 * 24 * 60 * 60, "7 days"),
    "2592000": (30 * 24 * 60 * 60, "30 days"),
}


def _share_expiry_from_form(expires_in: str, expires_at: str, *, now: int) -> tuple[int, str]:
    selected = str(expires_in or "604800").strip().lower()
    if selected == "custom":
        expiry_at = _parse_custom_share_expiry(expires_at)
        ttl_seconds = expiry_at - int(now)
        if ttl_seconds <= 0:
            raise ValueError("custom share expiry must be in the future")
        return ttl_seconds, "custom"
    if selected not in _SHARE_EXPIRY_PRESETS:
        raise ValueError("share expiry must be one of 1h, 1d, 7d, 30d, or custom")
    return _SHARE_EXPIRY_PRESETS[selected][0], selected


def _parse_custom_share_expiry(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("custom share expiry requires a date/time")
    if raw.isdigit():
        return int(raw)
    normalized = raw.removesuffix("Z") + ("+00:00" if raw.endswith("Z") else "")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("custom share expiry must be ISO-8601, e.g. 2030-01-02T03:04:05+00:00") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _share_expiry_summary(expires_at: Optional[int], *, expiry_kind: str, now: Optional[int] = None) -> tuple[str, str]:
    if expires_at is None:
        return "Expires when revoked", "No expiration timestamp is set."
    exact = datetime.fromtimestamp(int(expires_at), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if expiry_kind == "custom":
        return "Custom expiry", exact
    if expiry_kind in _SHARE_EXPIRY_PRESETS:
        return f"Expires in {_SHARE_EXPIRY_PRESETS[expiry_kind][1]}", exact
    checked_at = int(time.time()) if now is None else int(now)
    seconds = max(0, int(expires_at) - checked_at)
    for unit_seconds, unit_name in ((30 * 24 * 60 * 60, "month"), (24 * 60 * 60, "day"), (60 * 60, "hour"), (60, "minute")):
        if seconds >= unit_seconds and seconds % unit_seconds == 0:
            count = seconds // unit_seconds
            return f"Expires in {count} {unit_name}{'' if count == 1 else 's'}", exact
    return f"Expires at {exact}", exact


def _share_page(artifact: Artifact, token: str, request: Request, *, public_base_url: Optional[str] = None, expiry_kind: str = "604800") -> HTMLResponse:
    escaped_title = html.escape(artifact.title or artifact.slug)
    relative_path = f"/{artifact.slug}?share={token}"
    base_url = public_base_url or _external_base_url(request)
    share_url = f"{base_url}{relative_path}" if base_url else relative_path
    escaped_path = html.escape(share_url, quote=True)
    escaped_relative_path = html.escape(relative_path, quote=True)
    headline, exact_expiry = _share_expiry_summary(artifact.share_token_expires_at, expiry_kind=expiry_kind, now=artifact.updated_at)
    escaped_headline = html.escape(headline)
    escaped_exact_expiry = html.escape(exact_expiry)
    body = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Share · {escaped_title}</title>
        <style>
          :root {{ color-scheme: dark; --bg:#0b0f19; --panel:rgba(255,255,255,.08); --line:rgba(255,255,255,.14); --text:#eef2ff; --muted:#9aa7bd; --accent:#8b5cf6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; min-height:100vh; font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background:radial-gradient(circle at top left, rgba(139,92,246,.36), transparent 34rem), var(--bg); }}
          main {{ width:min(760px, calc(100% - 32px)); margin:0 auto; padding:72px 0; }}
          [data-artifactd-share-panel] {{ border:1px solid var(--line); border-radius:28px; background:linear-gradient(180deg, rgba(255,255,255,.10), rgba(255,255,255,.055)); box-shadow:0 28px 90px rgba(0,0,0,.34); padding:28px; }}
          .back, .pill {{ display:inline-flex; width:fit-content; border:1px solid var(--line); border-radius:999px; padding:9px 12px; color:var(--text); text-decoration:none; }}
          .eyebrow {{ margin:20px 0 8px; color:#c4b5fd; text-transform:uppercase; letter-spacing:.12em; font-size:.74rem; font-weight:900; }}
          h1 {{ margin:0; font-size:clamp(2.2rem, 7vw, 4rem); letter-spacing:-.06em; }}
          p {{ color:var(--muted); line-height:1.55; }}
          strong {{ color:var(--text); }}
          .expiry {{ display:flex; flex-wrap:wrap; gap:10px; margin:18px 0; }}
          .expiry .pill {{ background:rgba(139,92,246,.2); border-color:rgba(196,181,253,.5); color:#ede9fe; font-weight:800; }}
          .copy-row {{ display:flex; gap:10px; margin-top:18px; }}
          input {{ flex:1; min-width:0; border:1px solid var(--line); border-radius:16px; padding:14px 16px; background:rgba(255,255,255,.08); color:var(--text); font:inherit; }}
          button, .button {{ border:0; border-radius:999px; padding:12px 16px; background:var(--accent); color:white; font:inherit; font-weight:800; cursor:pointer; text-decoration:none; }}
          code {{ color:#c4b5fd; overflow-wrap:anywhere; }}
          .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }}
          .fine {{ font-size:.9rem; }}
        </style>
      </head>
      <body>
        <main>
          <section data-artifactd-share-panel>
            <a class="back" href="/">← Workspace Home</a>
            <p class="eyebrow">artifactd share</p>
            <h1>Share link created</h1>
            <p>This link unlocks only <strong>{escaped_title}</strong>. The workspace password stays private.</p>
            <div class="expiry" aria-label="Share expiry">
              <span class="pill">{escaped_headline}</span>
              <span class="pill">{escaped_exact_expiry}</span>
            </div>
            <div class="copy-row">
              <input id="share-url" value="{escaped_path}" readonly aria-label="Share URL">
              <button type="button" onclick="navigator.clipboard && navigator.clipboard.writeText(document.getElementById('share-url').value)">Copy link</button>
            </div>
            <div class="actions">
              <a class="button" href="{escaped_path}">Open share link</a>
              <a class="back" href="/{html.escape(artifact.slug, quote=True)}">Back to thing</a>
            </div>
            <p class="fine">Path: <code>{escaped_relative_path}</code></p>
          </section>
        </main>
      </body>
    </html>
    """
    return HTMLResponse(body)


def _external_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    if not host:
        return ""
    return f"{proto}://{host}".rstrip("/")


def _serve_artifact(store: ArtifactStore, slug: str, relative_path: str, request: Request, secret: str) -> Response:
    artifact = store.get(slug)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    session_cookie = _workspace_session_cookie(request, secret)
    if artifact.uses_profile_auth:
        share_token = request.query_params.get("share")
        if not store.verify_share_token(artifact.slug, share_token) and not session_cookie:
            return _password_page(artifact, status_code=401)
    elif artifact.password_hash:
        # Legacy per-artifact hashes are no longer accepted. A workspace session is required.
        if not session_cookie:
            return _password_page(artifact, status_code=401)
    try:
        file_path = store.resolve_file(artifact, relative_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if session_cookie and _is_html_file(file_path):
        try:
            body = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return FileResponse(file_path)
        csrf_token = _workspace_csrf_token(session_cookie, secret)
        return HTMLResponse(_inject_artifact_share_toolbar(body, artifact, csrf_token))
    return FileResponse(file_path)


def _is_html_file(file_path: Path) -> bool:
    return file_path.suffix.lower() in {".html", ".htm"}


def _inject_artifact_share_toolbar(body: str, artifact: Artifact, csrf_token: str) -> str:
    toolbar = _artifact_share_toolbar(artifact, csrf_token)
    lower_body = body.lower()
    marker = "</body>"
    index = lower_body.rfind(marker)
    if index == -1:
        return body + toolbar
    return body[:index] + toolbar + body[index:]


def _artifact_share_toolbar(artifact: Artifact, csrf_token: str) -> str:
    slug = html.escape(artifact.slug, quote=True)
    token = html.escape(csrf_token, quote=True)
    title = html.escape(artifact.title or artifact.slug)
    compact_select_style = "border:1px solid rgba(255,255,255,.18);border-radius:999px;padding:7px 8px;background:rgba(255,255,255,.10);color:white;font:inherit;"
    compact_datetime_style = "border:1px solid rgba(255,255,255,.18);border-radius:12px;padding:7px 8px;background:rgba(255,255,255,.10);color:white;font:inherit;"
    return f"""
    <style id="artifactd-share-toolbar-style">
      #artifactd-share-toolbar, #artifactd-share-toolbar * {{ box-sizing:border-box; }}
      #artifactd-share-toolbar {{ max-width:calc(100vw - 24px); max-height:calc(100vh - 24px); overflow:auto; }}
      #artifactd-share-toolbar .share-form {{ min-width:0; }}
      #artifactd-share-toolbar label {{ display:flex; align-items:center; gap:6px; min-width:0; flex:1 1 auto; }}
      #artifactd-share-toolbar .custom-share-expiry {{ flex:1 1 100%; min-width:0; color:#cbd5e1; }}
      @media (max-width: 560px) {{
        #artifactd-share-toolbar {{ left:12px; right:12px; bottom:12px; width:auto; }}
        #artifactd-share-toolbar > span {{ flex:1 1 100%; max-width:100%; padding-top:0; }}
        #artifactd-share-toolbar .share-form {{ flex:1 1 100%; max-width:100%; }}
        #artifactd-share-toolbar button, #artifactd-share-toolbar a {{ flex:0 0 auto; }}
      }}
    </style>
    <div id="artifactd-share-toolbar" style="position:fixed;right:16px;bottom:16px;z-index:2147483647;display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap;padding:10px 12px;border:1px solid rgba(255,255,255,.22);border-radius:20px;background:rgba(15,23,42,.94);box-shadow:0 18px 60px rgba(0,0,0,.35);color:white;font:14px/1.2 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <span style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c4b5fd;font-weight:700;padding-top:9px;">{title}</span>
      <a href="/" style="color:white;text-decoration:none;border:1px solid rgba(255,255,255,.18);border-radius:999px;padding:8px 10px;">Home</a>
      <form class="share-form" method="post" action="/_workspace/things/{slug}/share" style="margin:0;display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap;max-width:360px;">
        <input type="hidden" name="csrf_token" value="{token}">
        {_share_expiry_controls(select_style=compact_select_style, datetime_style=compact_datetime_style)}
        <button type="submit" style="border:0;border-radius:999px;padding:8px 12px;background:#8b5cf6;color:white;font:inherit;font-weight:800;cursor:pointer;">Share link</button>
      </form>
    </div>
    """


def _password_page(artifact: Artifact, *, status_code: int = 401, message: str = "Password required") -> HTMLResponse:
    escaped_slug = html.escape(artifact.slug)
    escaped_message = html.escape(message)
    auto_unlock = "false" if message == "Wrong password" else "true"
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
        <p>This artifact is protected by the workspace password.</p>
        <form method="post" action="/{escaped_slug}/login" data-master-password-form data-auto-unlock="{auto_unlock}">
          <input type="password" name="password" autocomplete="current-password" autofocus>
          <button type="submit">Unlock</button>
        </form>
        <script>
          (() => {{
            const key = 'artifactd.masterPassword';
            const form = document.querySelector('[data-master-password-form]');
            const input = form?.querySelector('input[name="password"]');
            const saved = window.localStorage?.getItem(key);
            if (saved && input) {{ input.value = saved; if (form?.dataset.autoUnlock === 'true') form.submit(); }}
            form?.addEventListener('submit', () => {{ if (input?.value) window.localStorage?.setItem(key, input.value); }});
          }})();
        </script>
      </body>
    </html>
    """
    return HTMLResponse(body, status_code=status_code)


def _cookie_name(slug: str) -> str:
    return f"artifactd_auth_{slug.replace('-', '_')}"


def _workspace_cookie_name() -> str:
    return "artifactd_workspace_auth"


def _has_workspace_session(request: Request, secret: str) -> bool:
    cookie = request.cookies.get(_workspace_cookie_name())
    return verify_artifact_cookie("__workspace__", cookie, secret)


def _workspace_password_page(*, status_code: int = 401, message: str = "Workspace password required") -> HTMLResponse:
    escaped_message = html.escape(message)
    auto_unlock = "false" if message == "Wrong password" else "true"
    body = f"""
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Workspace Home login</title></head>
      <body style="font-family: system-ui, sans-serif; max-width: 34rem; margin: 12vh auto; padding: 0 1rem;">
        <h1>{escaped_message}</h1>
        <p>One profile session unlocks protected generated Things in this Hermes workspace.</p>
        <form method="post" action="/_workspace/login" data-master-password-form data-auto-unlock="{auto_unlock}" style="display:flex;gap:.5rem;">
          <input type="password" name="password" autocomplete="current-password" autofocus style="flex:1;padding:.65rem .8rem;">
          <button type="submit" style="padding:.65rem .8rem;">Unlock</button>
        </form>
        <script>
          (() => {{
            const key = 'artifactd.masterPassword';
            const form = document.querySelector('[data-master-password-form]');
            const input = form?.querySelector('input[name="password"]');
            const saved = window.localStorage?.getItem(key);
            if (saved && input) {{ input.value = saved; if (form?.dataset.autoUnlock === 'true') form.submit(); }}
            form?.addEventListener('submit', () => {{ if (input?.value) window.localStorage?.setItem(key, input.value); }});
          }})();
        </script>
      </body>
    </html>
    """
    return HTMLResponse(body, status_code=status_code)


app = create_app()
