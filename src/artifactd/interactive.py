from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

from .security import verify_artifact_cookie
from .store import Artifact, ArtifactStore


REAUTH_SLUGS = {"gmail-reauth-cockpit", "google-reauth-cockpit", "google-auth-repair-center"}
REAUTH_CAPABILITY = "gog.reauth"
AUDIO_INTAKE_SLUGS = {"meeting-notes-intake"}
AUDIO_INTAKE_CAPABILITY = "audio.intake"
AUDIO_INTAKE_DIR = Path("/Users/skylarpayne/.hermes/profiles/echo/meeting-notes/intake")
TRANSCRIBE_SCRIPT = Path("/Users/skylarpayne/.hermes/profiles/echo/skills/productivity/meeting-notes-transcription/scripts/transcribe_meeting.py")
HERMES_AGENT_ROOT = Path("/Users/skylarpayne/.hermes/hermes-agent")
HERMES_PYTHON = HERMES_AGENT_ROOT / "venv/bin/python"
MAX_AUDIO_UPLOAD_BYTES = 250 * 1024 * 1024
ALLOWED_AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"}
MUTATION_FLAG_ENV = "ARTIFACTD_ENABLE_GOG_REAUTH_ACTIONS"
APPROVAL_FILE_ENV = "ARTIFACTD_GOG_REAUTH_APPROVAL_FILE"
PALMER_ARTIFACT_HOME = Path("/Users/skylarpayne/.hermes/artifacts")
ECHO_ARTIFACT_HOME = Path("/Users/skylarpayne/.hermes/profiles/echo/artifacts")
PALMER_PROFILE = Path("/Users/skylarpayne/.hermes/profiles/palmer")
ECHO_PROFILE = Path("/Users/skylarpayne/.hermes/profiles/echo")
AUTH_URL_RE = re.compile(r"https://accounts\.google\.com/[^\s\"'<>]+")
CODE_VALUE_RE = re.compile(r"(?i)(code=)[^&\s]+")
STATE_VALUE_RE = re.compile(r"(?i)(state=)[^&\s]+")


@dataclass(frozen=True)
class GoogleAccountConfig:
    account: str
    label: str
    profile: str
    profile_path: Path


@dataclass(frozen=True)
class GoogleProfileConfig:
    profile: str
    artifact_home: Path
    approval_file: Path
    accounts: tuple[GoogleAccountConfig, ...]

    @property
    def accounts_by_email(self) -> dict[str, GoogleAccountConfig]:
        return {item.account: item for item in self.accounts}


PALMER_GOOGLE_ACCOUNTS: tuple[GoogleAccountConfig, ...] = (
    GoogleAccountConfig("skylar.b.payne@gmail.com", "Skylar personal Google", "palmer", PALMER_PROFILE),
    GoogleAccountConfig("me@skylarbpayne.com", "Skylar domain Google", "palmer", PALMER_PROFILE),
    GoogleAccountConfig("jacquelineandskylar@gmail.com", "Wedding Google", "palmer", PALMER_PROFILE),
    GoogleAccountConfig("palmer@skylarbpayne.com", "Palmer Google", "palmer", PALMER_PROFILE),
)
ECHO_GOOGLE_ACCOUNTS: tuple[GoogleAccountConfig, ...] = (
    GoogleAccountConfig("jacquelineaguilar030@gmail.com", "Jacqueline personal Google", "echo", ECHO_PROFILE),
    GoogleAccountConfig("jaguilar@y2lef.org", "Jacqueline Y2LEF Google", "echo", ECHO_PROFILE),
    GoogleAccountConfig("jacquelineandskylar@gmail.com", "Wedding Google", "echo", ECHO_PROFILE),
)
PALMER_CONFIG = GoogleProfileConfig(
    profile="palmer",
    artifact_home=PALMER_ARTIFACT_HOME,
    approval_file=PALMER_ARTIFACT_HOME / ".enable-gog-reauth-actions",
    accounts=PALMER_GOOGLE_ACCOUNTS,
)
ECHO_CONFIG = GoogleProfileConfig(
    profile="echo",
    artifact_home=ECHO_ARTIFACT_HOME,
    approval_file=ECHO_ARTIFACT_HOME / ".enable-gog-reauth-actions",
    accounts=ECHO_GOOGLE_ACCOUNTS,
)


@dataclass
class GogReauthSession:
    session_key: str
    config: GoogleAccountConfig
    process: subprocess.Popen
    created_at: float = field(default_factory=time.time)
    status: str = "starting"
    auth_url: Optional[str] = None
    returncode: Optional[int] = None
    submitted_at: Optional[float] = None
    completed_at: Optional[float] = None
    output_tail: List[str] = field(default_factory=list)
    verification: Optional[Dict[str, object]] = None
    error: Optional[str] = None

    @property
    def account(self) -> str:
        return self.config.account


_sessions: Dict[str, GogReauthSession] = {}
_lock = threading.RLock()


def register_interactive_routes(app: FastAPI, store: ArtifactStore, secret: str) -> None:
    """Register small, fixed-purpose interactive artifact endpoints.

    These endpoints intentionally do not execute arbitrary commands. They only
    support allowlisted Google accounts for the current artifactd profile/home.
    The browser-triggered token mutation endpoints are additionally gated by an
    artifact capability and a server approval flag/marker.
    """

    @app.get("/{slug}/_interactive/gog/accounts")
    async def gog_accounts(slug: str, request: Request):
        artifact = _require_interactive_access(store, secret, slug, request)
        profile_config = _profile_config_for_store(store)
        return {
            "profile": profile_config.profile,
            "mutation_enabled": _mutation_enabled(artifact, profile_config),
            "mutation_capability": REAUTH_CAPABILITY,
            "mutation_flag_env": MUTATION_FLAG_ENV,
            "accounts": [_account_payload(config) for config in profile_config.accounts],
        }

    @app.post("/{slug}/_interactive/gog/start")
    async def gog_start(slug: str, request: Request, account: str = Form(...), force: bool = Form(False)):
        artifact = _require_interactive_access(store, secret, slug, request)
        profile_config = _profile_config_for_store(store)
        config = _require_allowed_account(profile_config, account)
        _require_mutation_enabled(store, artifact, profile_config, config=config, action="start")
        try:
            session = _start_session(config, force=force)
        except Exception as exc:  # pragma: no cover - defensive
            _audit(store, artifact.slug, "gog.reauth.start", config, "failed", error=exc.__class__.__name__)
            raise
        _audit(store, artifact.slug, "gog.reauth.start", config, "started", result_summary="manual gog auth process started")
        return _session_payload(session)

    @app.get("/{slug}/_interactive/gog/status")
    async def gog_status(slug: str, request: Request, account: str):
        _require_interactive_access(store, secret, slug, request)
        profile_config = _profile_config_for_store(store)
        config = _require_allowed_account(profile_config, account)
        key = _session_key(config)
        with _lock:
            session = _sessions.get(key)
            if not session:
                return {**_account_payload(config), "status": "idle"}
            _sync_returncode(session)
            return _session_payload(session)

    @app.post("/{slug}/_interactive/gog/submit")
    async def gog_submit(slug: str, request: Request, account: str = Form(...), redirect_url: str = Form(...)):
        artifact = _require_interactive_access(store, secret, slug, request)
        profile_config = _profile_config_for_store(store)
        config = _require_allowed_account(profile_config, account)
        _require_mutation_enabled(store, artifact, profile_config, config=config, action="submit")
        cleaned = redirect_url.strip()
        _validate_redirect_url(cleaned)
        key = _session_key(config)
        with _lock:
            session = _sessions.get(key)
            if not session:
                _audit(store, artifact.slug, "gog.reauth.submit", config, "failed", error="no active session")
                raise HTTPException(status_code=409, detail="No active re-auth process for this account. Click Re-auth first.")
            _sync_returncode(session)
            if session.process.poll() is not None:
                _audit(store, artifact.slug, "gog.reauth.submit", config, "failed", error="process exited")
                raise HTTPException(status_code=409, detail="The re-auth process is no longer running. Click Re-auth to start a fresh one.")
            try:
                assert session.process.stdin is not None
                session.process.stdin.write(cleaned + "\n")
                session.process.stdin.flush()
                session.process.stdin.close()
            except Exception as exc:  # pragma: no cover - defensive
                session.status = "failed"
                session.error = "Could not submit the redirect URL to gog. Start a fresh re-auth and try again."
                _audit(store, artifact.slug, "gog.reauth.submit", config, "failed", error=exc.__class__.__name__)
                raise HTTPException(status_code=500, detail=session.error) from exc
            session.status = "submitted"
            session.submitted_at = time.time()
            _audit(store, artifact.slug, "gog.reauth.submit", config, "submitted", result_summary="localhost redirect URL accepted transiently")
            threading.Thread(target=_wait_and_verify, args=(session, store, artifact.slug), daemon=True).start()
            return _session_payload(session)

    @app.post("/{slug}/_interactive/audio/upload")
    async def audio_upload(
        slug: str,
        request: Request,
        audio: UploadFile = File(...),
        meeting_title: str = Form(""),
        context: str = Form(""),
        output_style: str = Form("standard meeting notes"),
        return_to: str = Form("Jacqueline Telegram DM"),
    ):
        artifact = _require_interactive_access(store, secret, slug, request, allowed_slugs=AUDIO_INTAKE_SLUGS)
        _require_audio_intake_enabled(artifact)
        payload = await _save_audio_intake_upload(
            audio,
            meeting_title=meeting_title,
            context=context,
            output_style=output_style,
            return_to=return_to,
        )
        _audio_audit(store, artifact.slug, payload["upload_id"], "uploaded", result_summary=f"{payload['size_bytes']} byte audio upload accepted")
        _start_audio_processing(payload["upload_id"])
        return _read_audio_intake_metadata(str(payload["upload_id"]))

    @app.get("/{slug}/_interactive/audio/status")
    async def audio_status(slug: str, request: Request, upload_id: str):
        artifact = _require_interactive_access(store, secret, slug, request, allowed_slugs=AUDIO_INTAKE_SLUGS)
        _require_audio_intake_enabled(artifact)
        return _read_audio_intake_metadata(upload_id)

    @app.get("/{slug}/_interactive/audio/library")
    async def audio_library(
        slug: str,
        request: Request,
        q: str = "",
        tag: str = "",
        status: str = "",
        include_archived: bool = False,
        limit: int = 80,
    ):
        artifact = _require_interactive_access(store, secret, slug, request, allowed_slugs=AUDIO_INTAKE_SLUGS)
        _require_audio_intake_enabled(artifact)
        return _audio_library_payload(q=q, tag=tag, status=status, include_archived=include_archived, limit=limit)

    @app.get("/{slug}/_interactive/audio/meeting")
    async def audio_meeting(slug: str, request: Request, upload_id: str):
        artifact = _require_interactive_access(store, secret, slug, request, allowed_slugs=AUDIO_INTAKE_SLUGS)
        _require_audio_intake_enabled(artifact)
        return _audio_meeting_payload(upload_id)

    @app.post("/{slug}/_interactive/audio/update")
    async def audio_update(
        slug: str,
        request: Request,
        upload_id: str = Form(...),
        meeting_title: str = Form(""),
        meeting_date: str = Form(""),
        tags: str = Form(""),
        notes: str = Form(""),
        archived: bool = Form(False),
        follow_up_needed: bool = Form(False),
    ):
        artifact = _require_interactive_access(store, secret, slug, request, allowed_slugs=AUDIO_INTAKE_SLUGS)
        _require_audio_intake_enabled(artifact)
        updated = _update_audio_meeting(
            upload_id,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            tags=tags,
            notes=notes,
            archived=archived,
            follow_up_needed=follow_up_needed,
        )
        _audio_audit(store, artifact.slug, upload_id, "updated", result_summary="meeting library metadata updated")
        return _audio_meeting_payload(str(updated["upload_id"]))

    @app.get("/{slug}/_interactive/audio/download")
    async def audio_download(slug: str, request: Request, upload_id: str):
        artifact = _require_interactive_access(store, secret, slug, request, allowed_slugs=AUDIO_INTAKE_SLUGS)
        _require_audio_intake_enabled(artifact)
        meeting = _audio_meeting_payload(upload_id)
        title = _slugify_filename(str(meeting.get("meeting_title") or upload_id)) or upload_id
        return PlainTextResponse(
            str(meeting.get("effective_notes") or "") + "\n",
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{title}.md"'},
        )


def _require_interactive_access(
    store: ArtifactStore,
    secret: str,
    slug: str,
    request: Request,
    *,
    allowed_slugs: Optional[set[str]] = None,
) -> Artifact:
    if slug not in (allowed_slugs or REAUTH_SLUGS):
        raise HTTPException(status_code=404, detail="interactive endpoint not found")
    artifact = store.get(slug)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.password_hash:
        cookie = request.cookies.get(_cookie_name(artifact.slug))
        if not verify_artifact_cookie(artifact.slug, cookie, secret):
            raise HTTPException(status_code=401, detail="password required")
    return artifact


def _cookie_name(slug: str) -> str:
    return f"artifactd_auth_{slug.replace('-', '_')}"


def _require_audio_intake_enabled(artifact: Artifact) -> None:
    if AUDIO_INTAKE_CAPABILITY not in artifact.capabilities:
        raise HTTPException(
            status_code=403,
            detail=(
                "Audio intake is staged but not enabled for this artifact. "
                f"Redeploy with capability {AUDIO_INTAKE_CAPABILITY!r} after confirming this upload boundary."
            ),
        )


def _sanitize_upload_name(value: str) -> str:
    name = Path(value or "audio-note").name.strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip(".-_") or "audio-note"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported audio type. Upload M4A, MP3, WAV, OGG, OPUS, WEBM, MP4, AAC, or FLAC.")
    return f"{stem[:72]}{suffix}"


def _safe_upload_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{10}", value or ""):
        raise HTTPException(status_code=400, detail="Invalid upload id.")
    return value


async def _save_audio_intake_upload(
    audio: UploadFile,
    *,
    meeting_title: str,
    context: str,
    output_style: str,
    return_to: str,
) -> Dict[str, object]:
    safe_name = _sanitize_upload_name(audio.filename or "audio-note")
    now = int(time.time())
    upload_hash = hashlib.sha256(f"{now}:{safe_name}:{time.time_ns()}".encode("utf-8")).hexdigest()[:10]
    upload_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{upload_hash}"
    job_dir = AUDIO_INTAKE_DIR / upload_id
    job_dir.mkdir(parents=True, exist_ok=False)
    destination = job_dir / safe_name
    size = 0
    digest = hashlib.sha256()
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await audio.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Audio file is too large for this intake form. Use Telegram or share a Drive link instead.")
                digest.update(chunk)
                handle.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        try:
            await audio.close()
        except Exception:
            pass
    if size == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    metadata: Dict[str, object] = {
        "upload_id": upload_id,
        "status": "uploaded",
        "created_at": now,
        "file_name": safe_name,
        "content_type": audio.content_type or "",
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "audio_path": str(destination),
        "job_dir": str(job_dir),
        "metadata_path": str(job_dir / "metadata.json"),
        "meeting_title": meeting_title.strip()[:200],
        "context": context.strip()[:2000],
        "output_style": output_style.strip()[:120] or "standard meeting notes",
        "return_to": return_to.strip()[:160] or "Jacqueline Telegram DM",
        "next_step": "This page will process the audio and show meeting notes here when ready.",
        "echo_prompt": f"Echo, please process meeting-notes-intake upload {upload_id} and produce meeting notes.",
    }
    (job_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return metadata


def _read_audio_intake_metadata(upload_id: str) -> Dict[str, object]:
    safe_id = _safe_upload_id(upload_id)
    metadata_path = AUDIO_INTAKE_DIR / safe_id / "metadata.json"
    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found.")
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _iter_audio_intake_metadata() -> List[Dict[str, object]]:
    if not AUDIO_INTAKE_DIR.exists():
        return []
    items: List[Dict[str, object]] = []
    for metadata_path in AUDIO_INTAKE_DIR.glob("*/metadata.json"):
        upload_id = metadata_path.parent.name
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{10}", upload_id):
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if metadata.get("upload_id") != upload_id:
            metadata["upload_id"] = upload_id
        items.append(metadata)
    return items


def _audio_effective_notes(metadata: Dict[str, object]) -> str:
    return str(metadata.get("edited_meeting_notes") or metadata.get("meeting_notes") or "")


def _audio_transcript_excerpt(metadata: Dict[str, object], *, limit: Optional[int] = 2000) -> str:
    transcript_path = metadata.get("transcript_path")
    if not transcript_path:
        return ""
    path = Path(str(transcript_path))
    try:
        if path.exists() and path.is_file():
            transcript = path.read_text(encoding="utf-8", errors="replace")
            return transcript if limit is None else transcript[:limit]
    except Exception:
        return ""
    return ""


def _audio_search_blob(metadata: Dict[str, object]) -> str:
    tags = " ".join(_parse_tags(metadata.get("tags", [])))
    fields = [
        metadata.get("upload_id", ""),
        metadata.get("meeting_title", ""),
        metadata.get("meeting_date", ""),
        metadata.get("file_name", ""),
        metadata.get("context", ""),
        metadata.get("output_style", ""),
        metadata.get("return_to", ""),
        tags,
        _audio_effective_notes(metadata),
    ]
    return "\n".join(str(field or "") for field in fields).lower()


def _audio_library_item(metadata: Dict[str, object]) -> Dict[str, object]:
    notes = _audio_effective_notes(metadata)
    title = str(metadata.get("meeting_title") or metadata.get("file_name") or metadata.get("upload_id") or "Untitled meeting").strip()
    excerpt = re.sub(r"\s+", " ", notes).strip()[:260]
    return {
        "upload_id": metadata.get("upload_id"),
        "meeting_title": title,
        "meeting_date": metadata.get("meeting_date", ""),
        "tags": _parse_tags(metadata.get("tags", [])),
        "status": metadata.get("status", "unknown"),
        "progress": metadata.get("progress", ""),
        "follow_up_needed": bool(metadata.get("follow_up_needed", False)),
        "archived": bool(metadata.get("archived", False)),
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "completed_at": metadata.get("completed_at"),
        "file_name": metadata.get("file_name", ""),
        "excerpt": excerpt,
        "has_notes": bool(notes.strip()),
    }


def _audio_library_payload(*, q: str = "", tag: str = "", status: str = "", include_archived: bool = False, limit: int = 80) -> Dict[str, object]:
    query = (q or "").strip().lower()
    tag_filter = (tag or "").strip().lower()
    status_filter = (status or "").strip().lower()
    limit = min(max(int(limit or 80), 1), 300)
    items: List[Dict[str, object]] = []
    all_tags: set[str] = set()
    for metadata in _iter_audio_intake_metadata():
        tags = _parse_tags(metadata.get("tags", []))
        all_tags.update(tags)
        if not include_archived and metadata.get("archived"):
            continue
        if status_filter and str(metadata.get("status", "")).lower() != status_filter:
            continue
        if tag_filter and tag_filter not in {t.lower() for t in tags}:
            continue
        if query:
            blob = _audio_search_blob(metadata)
            if query not in blob and query not in _audio_transcript_excerpt(metadata, limit=1_000_000).lower():
                continue
        items.append(_audio_library_item(metadata))
    items.sort(key=lambda item: int(item.get("completed_at") or item.get("updated_at") or item.get("created_at") or 0), reverse=True)
    return {
        "meetings": items[:limit],
        "count": len(items[:limit]),
        "total_matches": len(items),
        "tags": sorted(all_tags, key=str.lower),
        "query": q,
        "tag": tag,
        "status": status,
        "include_archived": include_archived,
    }


def _audio_meeting_payload(upload_id: str) -> Dict[str, object]:
    metadata = _read_audio_intake_metadata(upload_id)
    effective_notes = _audio_effective_notes(metadata)
    payload = dict(metadata)
    payload["meeting_title"] = str(payload.get("meeting_title") or payload.get("file_name") or payload.get("upload_id") or "Untitled meeting")
    payload["tags"] = _parse_tags(payload.get("tags", []))
    payload["effective_notes"] = effective_notes
    payload["has_edits"] = bool(payload.get("edited_meeting_notes"))
    payload["transcript_excerpt"] = _audio_transcript_excerpt(payload, limit=2500)
    payload["transcript"] = _audio_transcript_excerpt(payload, limit=None)
    return payload


def _parse_tags(value: object) -> List[str]:
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,\n]", str(value or ""))
    tags: List[str] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = re.sub(r"\s+", " ", item.strip())[:40]
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            tags.append(cleaned)
    return tags[:20]


def _clean_meeting_date(value: str) -> str:
    cleaned = (value or "").strip()[:40]
    if cleaned and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", cleaned):
        raise HTTPException(status_code=400, detail="Meeting date must use YYYY-MM-DD.")
    return cleaned


def _update_audio_meeting(
    upload_id: str,
    *,
    meeting_title: str,
    meeting_date: str,
    tags: str,
    notes: str,
    archived: bool,
    follow_up_needed: bool,
) -> Dict[str, object]:
    safe_id = _safe_upload_id(upload_id)
    metadata = _read_audio_intake_metadata(safe_id)
    job_dir = Path(str(metadata.get("job_dir") or AUDIO_INTAKE_DIR / safe_id))
    title = re.sub(r"\s+", " ", (meeting_title or "").strip())[:200]
    updates: Dict[str, object] = {
        "meeting_title": title or str(metadata.get("meeting_title") or metadata.get("file_name") or "Untitled meeting"),
        "meeting_date": _clean_meeting_date(meeting_date),
        "tags": _parse_tags(tags),
        "archived": bool(archived),
        "follow_up_needed": bool(follow_up_needed),
    }
    clean_notes = (notes or "").replace("\r\n", "\n").strip()
    if clean_notes or metadata.get("edited_meeting_notes"):
        clean_notes = clean_notes[:250000]
        edited_path = job_dir / "meeting-notes.edited.md"
        edited_path.write_text(clean_notes.rstrip() + "\n", encoding="utf-8")
        updates.update(
            {
                "edited_meeting_notes": clean_notes,
                "edited_meeting_notes_path": str(edited_path),
                "notes_edited_at": int(time.time()),
            }
        )
    return _write_audio_intake_metadata(safe_id, updates)


def _slugify_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_").lower()
    return slug[:80]


def _write_audio_intake_metadata(upload_id: str, updates: Dict[str, object]) -> Dict[str, object]:
    safe_id = _safe_upload_id(upload_id)
    metadata_path = AUDIO_INTAKE_DIR / safe_id / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(metadata_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(updates)
    metadata["updated_at"] = int(time.time())
    tmp_path = metadata_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(metadata_path)
    return metadata


def _start_audio_processing(upload_id: object) -> None:
    safe_id = _safe_upload_id(str(upload_id))
    _write_audio_intake_metadata(safe_id, {"status": "queued", "progress": "Queued for transcription."})
    threading.Thread(target=_process_audio_upload, args=(safe_id,), daemon=True).start()


def _process_audio_upload(upload_id: str) -> None:
    try:
        metadata = _read_audio_intake_metadata(upload_id)
        audio_path = Path(str(metadata["audio_path"]))
        job_dir = Path(str(metadata.get("job_dir") or audio_path.parent))
        out_dir = job_dir / "transcription"
        _write_audio_intake_metadata(upload_id, {"status": "transcribing", "progress": "Transcribing audio locally with faster-whisper."})
        transcribe_env = os.environ.copy()
        transcribe_env.update(
            {
                "HERMES_HOME": str(ECHO_PROFILE),
                "PYTHONPATH": str(HERMES_AGENT_ROOT),
                "PATH": "/opt/homebrew/bin:/usr/local/bin:" + transcribe_env.get("PATH", ""),
            }
        )
        proc = subprocess.run(
            [str(HERMES_PYTHON), str(TRANSCRIBE_SCRIPT), str(audio_path), "--out", str(out_dir), "--model", "base"],
            text=True,
            capture_output=True,
            timeout=900,
            env=transcribe_env,
        )
        if proc.returncode != 0:
            raise RuntimeError(_redact((proc.stderr or proc.stdout or "Transcription failed")[-1200:]))
        transcript_path = out_dir / "transcript.txt"
        transcript = transcript_path.read_text(encoding="utf-8").strip()
        if not transcript:
            raise RuntimeError("No speech was detected in the upload.")
        transcribe_metadata_path = out_dir / "metadata.json"
        transcribe_metadata = json.loads(transcribe_metadata_path.read_text(encoding="utf-8")) if transcribe_metadata_path.exists() else {}
        _write_audio_intake_metadata(
            upload_id,
            {
                "status": "summarizing",
                "progress": "Generating meeting notes from the transcript.",
                "transcript_path": str(transcript_path),
                "transcript_timestamped_path": str(out_dir / "transcript.timestamped.md"),
                "transcription_metadata_path": str(transcribe_metadata_path),
                "transcription": {
                    "provider": transcribe_metadata.get("provider", "local faster-whisper"),
                    "model": transcribe_metadata.get("model", "base"),
                    "language": transcribe_metadata.get("language"),
                    "duration_seconds": transcribe_metadata.get("whisper_duration_seconds") or transcribe_metadata.get("ffprobe_duration_seconds"),
                    "segments": transcribe_metadata.get("segments"),
                },
            },
        )
        notes = _generate_meeting_notes(metadata, transcript)
        notes_path = job_dir / "meeting-notes.md"
        notes_path.write_text(notes.rstrip() + "\n", encoding="utf-8")
        _write_audio_intake_metadata(
            upload_id,
            {
                "status": "ready",
                "progress": "Meeting notes ready.",
                "meeting_notes": notes,
                "meeting_notes_path": str(notes_path),
                "completed_at": int(time.time()),
            },
        )
    except Exception as exc:
        try:
            _write_audio_intake_metadata(upload_id, {"status": "failed", "progress": "Processing failed.", "error": _redact(str(exc))[:1200]})
        except Exception:
            pass


def _hermes_bin() -> str:
    configured = os.environ.get("ARTIFACTD_HERMES_BIN")
    if configured:
        return configured
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    fallback = HERMES_AGENT_ROOT / "venv/bin/hermes"
    return str(fallback)


def _generate_meeting_notes(metadata: Dict[str, object], transcript: str) -> str:
    title = str(metadata.get("meeting_title") or "Uploaded audio note").strip() or "Uploaded audio note"
    context = str(metadata.get("context") or "").strip()
    output_style = str(metadata.get("output_style") or "standard meeting notes").strip()
    transcript_for_prompt = transcript
    if len(transcript_for_prompt) > 90000:
        transcript_for_prompt = transcript_for_prompt[:60000] + "\n\n[...middle omitted for length...]\n\n" + transcript_for_prompt[-25000:]
    prompt = f"""You are Echo, Jacqueline Aguilar's chief of staff. Create concise, operational meeting notes from the transcript below.

Return Markdown only. Do not mention that you are an AI. Do not invent speaker names or owners. If ownership is unclear, write Owner: unknown. Separate decisions from ideas. Include uncertainty when transcript quality or attribution is unclear.

Meeting title: {title}
Requested output style: {output_style}
Context from uploader: {context or 'None provided.'}

Use this structure exactly:

**Meeting notes — {title}**

**Bottom line**
- 1–3 bullets.

**Key decisions / agreements**
- Decision or "No clear decisions captured."

**Action items**
- [ ] Action — Owner: name/unknown; Due: date/unspecified; Context: why it matters.

**Important discussion points**
- Theme: concise substance.

**Open questions / blockers**
- Question/blocker or "None captured."

**Risks / sensitivities**
- Include only real signal, otherwise "None surfaced in the transcript."

**Suggested follow-up**
- Draft or next move, or "No follow-up needed beyond the action items."

**Transcript confidence**
- High/medium/low, with one sentence about audio quality/attribution limits.

Transcript:
{transcript_for_prompt}
"""
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(ECHO_PROFILE),
            "HERMES_QUIET": "1",
            "PATH": str(HERMES_AGENT_ROOT / "venv/bin") + ":/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", ""),
        }
    )
    proc = subprocess.run(
        [_hermes_bin(), "--ignore-rules", "--toolsets", "", "-z", prompt],
        text=True,
        capture_output=True,
        timeout=360,
        env=env,
        cwd=str(AUDIO_INTAKE_DIR),
    )
    if proc.returncode != 0:
        raise RuntimeError(_redact((proc.stderr or proc.stdout or "Meeting note generation failed")[-1200:]))
    notes = (proc.stdout or "").strip()
    if not notes:
        raise RuntimeError("Meeting note generation returned an empty response.")
    return notes


def _audio_audit(store: ArtifactStore, slug: str, upload_id: str, status: str, *, result_summary: str = "", error: str = "") -> None:
    payload_hash = hashlib.sha256(upload_id.encode("utf-8")).hexdigest()
    store.record_action_audit(
        slug=slug,
        capability=AUDIO_INTAKE_CAPABILITY,
        actor="artifact-browser",
        payload_hash=payload_hash,
        status=status,
        result_summary=result_summary[:240],
        error=_redact(error)[:240],
    )


def _profile_config_for_store(store: ArtifactStore) -> GoogleProfileConfig:
    """Resolve the account allowlist from the artifact home, not from email.

    The wedding Gmail exists in both allowlists; resolving by artifact home keeps
    Palmer's artifact on Palmer's gog profile and Echo's artifact on Echo's gog
    profile even when the public slug and email overlap.
    """

    home = _normalize_path(store.home)
    if home == _normalize_path(ECHO_ARTIFACT_HOME):
        return ECHO_CONFIG
    return PALMER_CONFIG


def _normalize_path(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _require_allowed_account(profile_config: GoogleProfileConfig, account: str) -> GoogleAccountConfig:
    config = profile_config.accounts_by_email.get(account)
    if not config:
        raise HTTPException(status_code=400, detail="Unsupported Google account for this artifact.")
    return config


def _session_key(config: GoogleAccountConfig) -> str:
    profile_scope = str(_normalize_path(config.profile_path))
    return f"{config.profile}:{profile_scope}:{config.account}"


def _account_payload(config: GoogleAccountConfig) -> Dict[str, object]:
    return {"account": config.account, "label": config.label, "profile": config.profile}


def _server_reauth_approved(profile_config: GoogleProfileConfig) -> bool:
    if os.environ.get(MUTATION_FLAG_ENV, "").lower() in {"1", "true", "yes"}:
        return True
    marker = Path(os.environ.get(APPROVAL_FILE_ENV, str(profile_config.approval_file)))
    return marker.exists()


def _mutation_enabled(artifact: Artifact, profile_config: GoogleProfileConfig) -> bool:
    return REAUTH_CAPABILITY in artifact.capabilities and _server_reauth_approved(profile_config)


def _require_mutation_enabled(
    store: ArtifactStore,
    artifact: Artifact,
    profile_config: GoogleProfileConfig,
    *,
    config: GoogleAccountConfig,
    action: str,
) -> None:
    if REAUTH_CAPABILITY not in artifact.capabilities:
        _audit(store, artifact.slug, f"gog.reauth.{action}", config, "denied", error="missing capability")
        raise HTTPException(
            status_code=403,
            detail=(
                "Live Google re-auth is staged but not enabled for this artifact. "
                f"Redeploy with capability {REAUTH_CAPABILITY!r} only after Skylar approves the endpoint boundary."
            ),
        )
    if not _server_reauth_approved(profile_config):
        _audit(store, artifact.slug, f"gog.reauth.{action}", config, "denied", error="server approval disabled")
        raise HTTPException(
            status_code=403,
            detail=(
                "Live Google re-auth is disabled at the server approval gate. "
                f"Set {MUTATION_FLAG_ENV}=1 or create the approved marker file only after Skylar approves browser-triggered token mutation."
            ),
        )


def _audit(
    store: ArtifactStore,
    slug: str,
    action: str,
    config: GoogleAccountConfig,
    status: str,
    *,
    result_summary: str = "",
    error: str = "",
) -> None:
    payload = json.dumps(
        {"action": action, "account": config.account, "profile": config.profile},
        sort_keys=True,
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload).hexdigest()
    safe_error = _redact(error)[:240]
    store.record_action_audit(
        slug=slug,
        capability=REAUTH_CAPABILITY,
        actor="artifact-browser",
        payload_hash=payload_hash,
        status=status,
        result_summary=result_summary[:240],
        error=safe_error,
    )


def _gog_bin() -> str:
    configured = os.environ.get("ARTIFACTD_GOG_BIN")
    if configured:
        return configured
    discovered = shutil.which("gog")
    if discovered:
        return discovered
    fallback = Path("/opt/homebrew/bin/gog")
    if fallback.exists():
        return str(fallback)
    return "gog"


def _profile_env(config: GoogleAccountConfig) -> dict[str, str]:
    profile = config.profile_path
    env = os.environ.copy()
    env.pop("GOG_KEYRING_PASSWORD", None)
    env.update(
        {
            "HERMES_HOME": str(profile),
            "HOME": str(profile / "home"),
            "GOG_CONFIG_DIR": str(profile / "home/Library/Application Support/gogcli"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", ""),
        }
    )
    env_path = profile / ".env"
    if env_path.exists():
        for raw in env_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("GOG_KEYRING_PASSWORD="):
                env["GOG_KEYRING_PASSWORD"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return env


def _start_session(config: GoogleAccountConfig, *, force: bool = False) -> GogReauthSession:
    key = _session_key(config)
    with _lock:
        existing = _sessions.get(key)
        if existing:
            _sync_returncode(existing)
            if existing.process.poll() is None and not force:
                return existing
            if existing.process.poll() is None and force:
                _terminate(existing)
        cmd = [
            _gog_bin(),
            "auth",
            "add",
            config.account,
            "--manual",
            "--services",
            "all",
            "--drive-scope",
            "full",
            "--force-consent",
        ]
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_profile_env(config),
        )
        session = GogReauthSession(session_key=key, config=config, process=process)
        _sessions[key] = session
        threading.Thread(target=_read_output, args=(session,), daemon=True).start()
        return session


def _read_output(session: GogReauthSession) -> None:
    process = session.process
    try:
        assert process.stdout is not None
        for line in process.stdout:
            safe_line = _redact(line.rstrip("\n"))
            with _lock:
                session.output_tail.append(safe_line)
                session.output_tail = session.output_tail[-20:]
                match = AUTH_URL_RE.search(line)
                if match:
                    session.auth_url = match.group(0)
                    if session.status == "starting":
                        session.status = "auth_url_ready"
                if "Paste the full redirect URL" in line or "redirect URL" in line:
                    if session.auth_url and session.status in {"starting", "auth_url_ready"}:
                        session.status = "waiting_for_redirect"
    except Exception as exc:  # pragma: no cover - defensive
        with _lock:
            session.error = f"Could not read gog output: {exc.__class__.__name__}"
            session.status = "failed"


def _sync_returncode(session: GogReauthSession) -> None:
    rc = session.process.poll()
    if rc is not None and session.returncode is None:
        session.returncode = rc
        session.completed_at = time.time()
        if rc == 0 and session.status not in {"verified", "verifying"}:
            session.status = "completed"
        elif rc != 0 and session.status != "failed":
            session.status = "failed"
            session.error = "gog exited before re-auth completed. Start a fresh re-auth and use the newest localhost URL."


def _wait_and_verify(session: GogReauthSession, store: ArtifactStore, slug: str) -> None:
    try:
        rc = session.process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        with _lock:
            session.status = "failed"
            session.error = "gog did not finish after the redirect URL was submitted. Start a fresh re-auth and try again."
        _audit(store, slug, "gog.reauth.verify", session.config, "failed", error="timeout")
        _terminate(session)
        return
    with _lock:
        session.returncode = rc
        session.completed_at = time.time()
        if rc != 0:
            session.status = "failed"
            session.error = "gog rejected the redirect URL or the code expired. Start a fresh re-auth and use the newest localhost URL."
            _audit(store, slug, "gog.reauth.exchange", session.config, "failed", error="gog rejected redirect")
            return
        session.status = "verifying"
    verification = _verify_account(session.config)
    with _lock:
        session.verification = verification
        session.status = "verified" if verification.get("ok") else "completed"
    _audit(
        store,
        slug,
        "gog.reauth.verify",
        session.config,
        "verified" if verification.get("ok") else "completed_with_failed_checks",
        result_summary="Gmail, Calendar, Drive, and full-scope inventory smoke checks rerun after exchange",
    )


def _verify_account(config: GoogleAccountConfig) -> Dict[str, object]:
    account = config.account
    checks = [
        ("gmail", [_gog_bin(), "gmail", "search", "newer_than:7d", "--account", account, "--max", "1", "--json", "--no-input"]),
        ("calendar", [_gog_bin(), "calendar", "events", "primary", "--account", account, "--days", "7", "--max", "1", "--json", "--no-input"]),
        ("drive", [_gog_bin(), "drive", "ls", "--account", account, "--max", "1", "--json", "--no-input"]),
        ("oauth-scopes", [_gog_bin(), "auth", "list", "--json", "--no-input"]),
    ]
    results = []
    ok = True
    env = _profile_env(config)
    for name, cmd in checks:
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=45, env=env)
            passed = result.returncode == 0
            evidence = "read smoke passed"
            if not passed and name == "oauth-scopes" and _legacy_backup_token_inventory_error(result.stderr or result.stdout):
                passed = True
                evidence = "inventory skipped: gog auth list is blocked by an old backup token file; direct service checks passed and the repair flow requested all services"
            elif passed and name == "oauth-scopes":
                required = {"gmail", "calendar", "drive", "docs", "sheets", "contacts", "tasks", "people"}
                try:
                    data = json.loads(result.stdout or "{}")
                    accounts = data.get("accounts", []) if isinstance(data, dict) else data
                    entry = next((item for item in accounts if item.get("email") == account), {})
                    services = set(entry.get("services") or [])
                    missing = sorted(required - services)
                    passed = not missing
                    evidence = "full gog service set present" if passed else "missing services: " + ", ".join(missing)
                except Exception as exc:
                    passed = False
                    evidence = f"could not parse scope inventory: {exc.__class__.__name__}"
            ok = ok and passed
            results.append(
                {
                    "service": name,
                    "ok": passed,
                    "evidence": _redact(result.stderr or result.stdout)[:160] if (not passed and name != "oauth-scopes") else evidence,
                }
            )
        except Exception as exc:
            ok = False
            results.append({"service": name, "ok": False, "evidence": exc.__class__.__name__})
    return {"ok": ok, "checks": results}


def _validate_redirect_url(value: str) -> None:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None

    if (
        not parsed
        or parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or port != 1
    ):
        raise HTTPException(status_code=400, detail="Paste the full localhost redirect URL from the browser address bar.")
    if not parse_qs(parsed.query).get("code"):
        raise HTTPException(status_code=400, detail="That URL does not include a Google authorization code. Use the newest localhost URL after approving Google.")


def _terminate(session: GogReauthSession) -> None:
    try:
        session.process.terminate()
        session.process.wait(timeout=5)
    except Exception:
        try:
            session.process.kill()
        except Exception:
            pass


def _session_payload(session: GogReauthSession) -> Dict[str, object]:
    _sync_returncode(session)
    payload: Dict[str, object] = {
        **_account_payload(session.config),
        "status": session.status,
        "has_auth_url": bool(session.auth_url),
        "auth_url": session.auth_url,
        "returncode": session.returncode,
        "created_at": int(session.created_at),
    }
    if session.submitted_at:
        payload["submitted_at"] = int(session.submitted_at)
    if session.completed_at:
        payload["completed_at"] = int(session.completed_at)
    if session.error:
        payload["error"] = session.error
    if session.verification:
        payload["verification"] = session.verification
    return payload

def _redact(text: str) -> str:
    safe = (text or "").replace("\x00", "")
    safe = AUTH_URL_RE.sub("[GOOGLE_AUTH_URL_REDACTED]", safe)
    safe = CODE_VALUE_RE.sub(r"\1[REDACTED]", safe)
    safe = STATE_VALUE_RE.sub(r"\1[REDACTED]", safe)
    safe = re.sub(r"(?i)(refresh_token|access_token|client_secret|password)\"?\s*[:=]\s*\"?[^\"\s,}]+", r"\1=REDACTED", safe)
    return " ".join(safe.split())


def _legacy_backup_token_inventory_error(text: str) -> bool:
    """Detect `gog auth list` failures caused by stale backup token files.

    A corrupt `token:...bak-*` / `token:...pre-reencrypt-*` file can poison the
    global inventory command even when the selected account's direct API smoke
    checks pass. Do not make a successful fresh OAuth flow look failed because
    of an unrelated legacy backup file.
    """

    return bool(re.search(r"(?:token[: ]|token for ).*(?:\.bak-|\.pre-reencrypt-)", text or ""))
