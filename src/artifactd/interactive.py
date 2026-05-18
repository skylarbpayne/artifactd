from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException, Request

from .security import verify_artifact_cookie
from .store import ArtifactStore


REAUTH_SLUGS = {"gmail-reauth-cockpit", "google-reauth-cockpit"}
GOOGLE_ACCOUNTS = {
    "jacquelineaguilar030@gmail.com": "Personal Google",
    "jaguilar@y2lef.org": "Y2LEF Google",
}
AUTH_URL_RE = re.compile(r"https://accounts\.google\.com/[^\s\"'<>]+")
CODE_VALUE_RE = re.compile(r"(?i)(code=)[^&\s]+")
STATE_VALUE_RE = re.compile(r"(?i)(state=)[^&\s]+")


@dataclass
class GogReauthSession:
    account: str
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


_sessions: Dict[str, GogReauthSession] = {}
_lock = threading.RLock()


def register_interactive_routes(app: FastAPI, store: ArtifactStore, secret: str) -> None:
    """Register small, fixed-purpose interactive artifact endpoints.

    These endpoints intentionally do not execute arbitrary commands. They only
    support Jacqueline's two known Google accounts and only when accessed through
    the protected Google re-auth artifact.
    """

    @app.get("/{slug}/_interactive/gog/accounts")
    async def gog_accounts(slug: str, request: Request):
        _require_interactive_access(store, secret, slug, request)
        return {"accounts": [{"account": account, "label": label} for account, label in GOOGLE_ACCOUNTS.items()]}

    @app.post("/{slug}/_interactive/gog/start")
    async def gog_start(slug: str, request: Request, account: str = Form(...), force: bool = Form(False)):
        _require_interactive_access(store, secret, slug, request)
        _require_allowed_account(account)
        session = _start_session(account, force=force)
        return _session_payload(session)

    @app.get("/{slug}/_interactive/gog/status")
    async def gog_status(slug: str, request: Request, account: str):
        _require_interactive_access(store, secret, slug, request)
        _require_allowed_account(account)
        with _lock:
            session = _sessions.get(account)
            if not session:
                return {"account": account, "label": GOOGLE_ACCOUNTS[account], "status": "idle"}
            _sync_returncode(session)
            return _session_payload(session)

    @app.post("/{slug}/_interactive/gog/submit")
    async def gog_submit(slug: str, request: Request, account: str = Form(...), redirect_url: str = Form(...)):
        _require_interactive_access(store, secret, slug, request)
        _require_allowed_account(account)
        cleaned = redirect_url.strip()
        _validate_redirect_url(cleaned)
        with _lock:
            session = _sessions.get(account)
            if not session:
                raise HTTPException(status_code=409, detail="No active re-auth process for this account. Click Re-auth first.")
            _sync_returncode(session)
            if session.process.poll() is not None:
                raise HTTPException(status_code=409, detail="The re-auth process is no longer running. Click Re-auth to start a fresh one.")
            try:
                assert session.process.stdin is not None
                session.process.stdin.write(cleaned + "\n")
                session.process.stdin.flush()
                session.process.stdin.close()
            except Exception as exc:  # pragma: no cover - defensive
                session.status = "failed"
                session.error = "Could not submit the redirect URL to gog. Start a fresh re-auth and try again."
                raise HTTPException(status_code=500, detail=session.error) from exc
            session.status = "submitted"
            session.submitted_at = time.time()
            threading.Thread(target=_wait_and_verify, args=(session,), daemon=True).start()
            return _session_payload(session)


def _require_interactive_access(store: ArtifactStore, secret: str, slug: str, request: Request) -> None:
    if slug not in REAUTH_SLUGS:
        raise HTTPException(status_code=404, detail="interactive endpoint not found")
    artifact = store.get(slug)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.password_hash:
        cookie = request.cookies.get(_cookie_name(artifact.slug))
        if not verify_artifact_cookie(artifact.slug, cookie, secret):
            raise HTTPException(status_code=401, detail="password required")


def _cookie_name(slug: str) -> str:
    return f"artifactd_auth_{slug.replace('-', '_')}"


def _require_allowed_account(account: str) -> None:
    if account not in GOOGLE_ACCOUNTS:
        raise HTTPException(status_code=400, detail="Unsupported Google account for this artifact.")


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


def _start_session(account: str, *, force: bool = False) -> GogReauthSession:
    with _lock:
        existing = _sessions.get(account)
        if existing:
            _sync_returncode(existing)
            if existing.process.poll() is None and not force:
                return existing
            if existing.process.poll() is None and force:
                _terminate(existing)
        cmd = [_gog_bin(), "auth", "add", account, "--manual", "--services", "all", "--force-consent"]
        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        session = GogReauthSession(account=account, process=process)
        _sessions[account] = session
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


def _wait_and_verify(session: GogReauthSession) -> None:
    try:
        rc = session.process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        with _lock:
            session.status = "failed"
            session.error = "gog did not finish after the redirect URL was submitted. Start a fresh re-auth and try again."
        _terminate(session)
        return
    with _lock:
        session.returncode = rc
        session.completed_at = time.time()
        if rc != 0:
            session.status = "failed"
            session.error = "gog rejected the redirect URL or the code expired. Start a fresh re-auth and use the newest localhost URL."
            return
        session.status = "verifying"
    verification = _verify_account(session.account)
    with _lock:
        session.verification = verification
        session.status = "verified" if verification.get("ok") else "completed"


def _verify_account(account: str) -> Dict[str, object]:
    checks = [
        ("gmail", [_gog_bin(), "--account", account, "gmail", "search", "newer_than:7d", "--max", "1", "--json", "--no-input"]),
        ("calendar", [_gog_bin(), "--account", account, "calendar", "events", "primary", "--days", "7", "--max", "1", "--json", "--no-input"]),
        ("drive", [_gog_bin(), "--account", account, "drive", "search", "trashed=false", "--max", "1", "--json", "--no-input"]),
    ]
    results = []
    ok = True
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    for name, cmd in checks:
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=45, env=env)
            passed = result.returncode == 0
            ok = ok and passed
            results.append({"service": name, "ok": passed})
        except Exception:
            ok = False
            results.append({"service": name, "ok": False})
    return {"ok": ok, "checks": results}


def _validate_redirect_url(value: str) -> None:
    if not (value.startswith("http://localhost:1/") or value.startswith("http://127.0.0.1:1/")):
        raise HTTPException(status_code=400, detail="Paste the full localhost redirect URL from the browser address bar.")
    if "code=" not in value:
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
        "account": session.account,
        "label": GOOGLE_ACCOUNTS[session.account],
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
    text = AUTH_URL_RE.sub("[GOOGLE_AUTH_URL_REDACTED]", text)
    text = CODE_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = STATE_VALUE_RE.sub(r"\1[REDACTED]", text)
    return text
