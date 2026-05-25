from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request

from .security import verify_artifact_cookie
from .store import Artifact, ArtifactStore, sanitize_slug

MAX_STATE_PAYLOAD_BYTES = int(os.environ.get("ARTIFACTD_MAX_STATE_PAYLOAD_BYTES", str(2 * 1024 * 1024)))
_STATE_KEY_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")


def register_state_routes(app: FastAPI, store: ArtifactStore, secret: str) -> None:
    @app.get("/{slug}/_state/{key}")
    async def get_artifact_state(slug: str, key: str, request: Request) -> dict[str, Any]:
        artifact = _require_state_access(store, secret, slug, request, mutate=False)
        safe_key = _safe_state_key(key)
        record = _read_state(store, artifact.slug, safe_key)
        if record is None:
            return {"ok": True, "slug": artifact.slug, "key": safe_key, "exists": False, "version": 0, "snapshot": None}
        return {"ok": True, "slug": artifact.slug, "key": safe_key, "exists": True, **record}

    @app.put("/{slug}/_state/{key}")
    async def put_artifact_state(slug: str, key: str, request: Request) -> dict[str, Any]:
        artifact = _require_state_access(store, secret, slug, request, mutate=True)
        safe_key = _safe_state_key(key)
        if _payload_too_large(request):
            raise HTTPException(status_code=413, detail="state payload is too large")
        payload = await _json_payload(request)
        if "snapshot" not in payload:
            raise HTTPException(status_code=422, detail="snapshot is required")
        client_id = payload.get("client_id")
        if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 120):
            raise HTTPException(status_code=422, detail="client_id must be a short string")
        expected_version = payload.get("expected_version")
        if expected_version is not None and (not isinstance(expected_version, int) or expected_version < 0):
            raise HTTPException(status_code=422, detail="expected_version must be a non-negative integer")
        record = _write_state(
            store,
            artifact.slug,
            safe_key,
            snapshot=payload["snapshot"],
            client_id=client_id,
            expected_version=expected_version,
        )
        return {"ok": True, "slug": artifact.slug, "key": safe_key, **record}

    @app.post("/{slug}/_state/{key}")
    async def post_artifact_state(slug: str, key: str, request: Request) -> dict[str, Any]:
        return await put_artifact_state(slug, key, request)


def _require_state_access(store: ArtifactStore, secret: str, slug: str, request: Request, *, mutate: bool) -> Artifact:
    artifact = store.get(slug)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.is_archived:
        raise HTTPException(status_code=410, detail="artifact is archived")
    if "artifact.state" not in artifact.capabilities:
        raise HTTPException(status_code=403, detail="artifact.state capability is not enabled for this artifact")

    # Public artifacts with artifact.state are intentionally world-writable spike surfaces.
    # Protected artifacts require either a workspace session cookie or a valid share token.
    if artifact.uses_profile_auth or artifact.password_hash:
        session_cookie = request.cookies.get("artifactd_workspace_auth")
        share_token = request.query_params.get("share")
        has_workspace_session = verify_artifact_cookie("__workspace__", session_cookie, secret)
        has_share_session = bool(share_token and store.verify_share_token(artifact.slug, share_token))
        if not has_workspace_session and not has_share_session:
            raise HTTPException(status_code=401, detail="workspace password or share token required")
    return artifact


def _safe_state_key(key: str) -> str:
    if not _STATE_KEY_RE.fullmatch(str(key or "")):
        raise HTTPException(status_code=422, detail="state key must be 1-80 chars: letters, numbers, underscore, dash, dot")
    return key


def _state_path(store: ArtifactStore, slug: str, key: str) -> Path:
    safe_slug = sanitize_slug(slug)
    safe_key = _safe_state_key(key)
    return store.home / "state" / safe_slug / f"{safe_key}.json"


def _read_state(store: ArtifactStore, slug: str, key: str) -> Optional[dict[str, Any]]:
    path = _state_path(store, slug, key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="state file is corrupt") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="state file is invalid")
    return data


def _write_state(
    store: ArtifactStore,
    slug: str,
    key: str,
    *,
    snapshot: Any,
    client_id: Optional[str],
    expected_version: Optional[int],
) -> dict[str, Any]:
    path = _state_path(store, slug, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_state(store, slug, key)
    current_version = int(existing.get("version", 0)) if existing else 0
    if expected_version is not None and current_version != expected_version:
        raise HTTPException(status_code=409, detail={"message": "state version conflict", "version": current_version})
    now = int(time.time())
    record = {
        "version": current_version + 1,
        "updated_at": now,
        "updated_by": client_id or "anonymous",
        "snapshot": snapshot,
    }
    tmp_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_STATE_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="state payload is too large")
    tmp_path.write_text(encoded, encoding="utf-8")
    tmp_path.replace(path)
    return record


def _payload_too_large(request: Request) -> bool:
    raw_length = request.headers.get("content-length")
    if not raw_length:
        return False
    try:
        return int(raw_length) > MAX_STATE_PAYLOAD_BYTES
    except ValueError:
        return True


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="state payload must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="state payload must be a JSON object")
    return payload
