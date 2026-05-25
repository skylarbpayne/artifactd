from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request

from .security import sign_csrf_token, verify_artifact_cookie, verify_csrf_token
from .store import Artifact, ArtifactStore

MAX_ACTION_PAYLOAD_BYTES = 16 * 1024
_TASK_ID_RE = re.compile(r"^t_[A-Za-z0-9]+$")
_ASSIGNEE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[ArtifactStore, Artifact, dict[str, Any], "KanbanExecutor"], dict[str, Any]]
    provider: str = "artifactd"
    executes_via: str = "artifactd sidecar"
    approval_required: bool = False


class KanbanExecutor:
    def __init__(self, profile: Optional[str] = None):
        self.profile = profile or _default_profile()

    @property
    def actor(self) -> str:
        return f"hermes-profile:{self.profile}"

    def comment(self, task_id: str, body: str) -> dict[str, Any]:
        completed = subprocess.run(
            ["hermes", "-p", self.profile, "kanban", "comment", task_id, body],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {"task_id": task_id, "stdout": completed.stdout.strip()}

    def create_task(
        self,
        *,
        title: str,
        assignee: str,
        body: str = "",
        parents: Optional[list[str]] = None,
        priority: Optional[int] = None,
    ) -> dict[str, Any]:
        cmd = ["hermes", "-p", self.profile, "kanban", "create", title, "--assignee", assignee]
        if body:
            cmd.extend(["--body", body])
        for parent in parents or []:
            cmd.extend(["--parent", parent])
        if priority is not None:
            cmd.extend(["--priority", str(priority)])
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        return {"stdout": completed.stdout.strip()}


ApprovalGateExecutor = KanbanExecutor


def register_action_routes(app: FastAPI, store: ArtifactStore, secret: str, *, kanban_executor: Optional[KanbanExecutor] = None) -> None:
    executor = kanban_executor or KanbanExecutor()

    @app.get("/{slug}/_actions")
    async def action_manifest(slug: str, request: Request):
        artifact, session_cookie = _require_action_session(store, secret, slug, request)
        allowed = [_capability_payload(capability, executor) for capability in _artifact_capabilities(artifact)]
        return {"slug": artifact.slug, "csrf_token": sign_csrf_token(artifact.slug, session_cookie, secret), "capabilities": allowed}

    @app.post("/{slug}/_actions/{capability_name}")
    async def run_action(slug: str, capability_name: str, request: Request):
        artifact, session_cookie = _require_action_session(store, secret, slug, request)
        capability = CAPABILITY_REGISTRY.get(capability_name)
        if not capability or capability_name not in artifact.capabilities:
            raise HTTPException(status_code=403, detail="capability not allowed for this artifact")
        if _payload_too_large(request):
            raise HTTPException(status_code=413, detail="action payload is too large")
        payload = await _json_payload(request)
        payload_hash = _payload_hash(payload)
        token = request.headers.get("x-artifactd-csrf") or payload.get("_csrf")
        if not verify_csrf_token(artifact.slug, token, session_cookie, secret):
            store.record_action_audit(
                slug=artifact.slug,
                capability=capability.name,
                actor=_audit_actor(capability, executor),
                payload_hash=payload_hash,
                status="denied",
                error="csrf verification failed",
            )
            raise HTTPException(status_code=403, detail="CSRF token is missing or invalid")
        if capability.approval_required:
            store.record_action_audit(
                slug=artifact.slug,
                capability=capability.name,
                actor=_audit_actor(capability, executor),
                payload_hash=payload_hash,
                status="approval_required",
                error="external or destructive actions require approval",
            )
            raise HTTPException(status_code=403, detail="approval required before this action can run")
        try:
            clean_payload = _validate_payload(capability.name, payload)
            result = capability.handler(store, artifact, clean_payload, executor)
        except HTTPException as exc:
            store.record_action_audit(
                slug=artifact.slug,
                capability=capability.name,
                actor=_audit_actor(capability, executor),
                payload_hash=payload_hash,
                status="error",
                error=str(exc.detail),
            )
            raise
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "kanban command failed").strip()
            store.record_action_audit(
                slug=artifact.slug,
                capability=capability.name,
                actor=_audit_actor(capability, executor),
                payload_hash=payload_hash,
                status="error",
                error=error[:500],
            )
            raise HTTPException(status_code=502, detail="kanban command failed") from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            store.record_action_audit(
                slug=artifact.slug,
                capability=capability.name,
                actor=_audit_actor(capability, executor),
                payload_hash=payload_hash,
                status="error",
                error=exc.__class__.__name__,
            )
            raise HTTPException(status_code=500, detail="action failed") from exc
        store.record_action_audit(
            slug=artifact.slug,
            capability=capability.name,
            actor=_audit_actor(capability, executor),
            payload_hash=payload_hash,
            status="ok",
            result_summary=_result_summary(result),
        )
        return {"ok": True, "capability": capability.name, "result": result}


def _require_action_session(store: ArtifactStore, secret: str, slug: str, request: Request) -> tuple[Artifact, str]:
    artifact = store.get(slug)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.is_archived:
        raise HTTPException(status_code=410, detail="artifact is archived")
    if artifact.uses_profile_auth or artifact.password_hash:
        session_cookie = request.cookies.get(_workspace_cookie_name())
        if not verify_artifact_cookie("__workspace__", session_cookie, secret):
            raise HTTPException(status_code=401, detail="workspace password required")
    else:
        raise HTTPException(status_code=403, detail="action capabilities require a protected artifact or workspace session")
    return artifact, session_cookie or ""


def _cookie_name(slug: str) -> str:
    return f"artifactd_auth_{slug.replace('-', '_')}"


def _workspace_cookie_name() -> str:
    return "artifactd_workspace_auth"


def _default_profile() -> str:
    for key in ("ARTIFACTD_PROFILE", "HERMES_PROFILE"):
        value = os.environ.get(key)
        if value:
            return value
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        name = str(hermes_home).rstrip("/").split("/")[-1]
        if name:
            return name
    return "default"


def _artifact_capabilities(artifact: Artifact) -> list[Capability]:
    return [CAPABILITY_REGISTRY[name] for name in artifact.capabilities if name in CAPABILITY_REGISTRY]


def _capability_payload(capability: Capability, executor: KanbanExecutor) -> dict[str, Any]:
    payload = {
        "name": capability.name,
        "description": capability.description,
        "schema": capability.schema,
        "provider": capability.provider,
        "executes_via": capability.executes_via,
        "approval_required": capability.approval_required,
    }
    if capability.provider == "hermes-profile":
        payload["profile"] = getattr(executor, "profile", _default_profile())
    return payload


def _audit_actor(capability: Capability, executor: KanbanExecutor) -> str:
    if capability.provider == "hermes-profile":
        return str(getattr(executor, "actor", f"hermes-profile:{getattr(executor, 'profile', _default_profile())}"))
    return "artifact-session"


def _payload_too_large(request: Request) -> bool:
    raw_length = request.headers.get("content-length")
    if not raw_length:
        return False
    try:
        return int(raw_length) > MAX_ACTION_PAYLOAD_BYTES
    except ValueError:
        return True


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="action payload must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="action payload must be a JSON object")
    return payload


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_payload(capability_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if capability_name == "artifact.describe":
        title = _optional_string(payload, "title", max_length=200)
        description = _optional_string(payload, "description", max_length=2000)
        if title is None and description is None:
            raise HTTPException(status_code=422, detail="provide title, description, or both")
        return {"title": title, "description": description}
    if capability_name == "artifact.archive":
        return {"reason": _optional_string(payload, "reason", max_length=500) or "Archived from artifact action"}
    if capability_name == "kanban.comment":
        task_id = _required_task_id(payload, "task_id")
        body = _required_string(payload, "body", max_length=5000)
        return {"task_id": task_id, "body": body}
    if capability_name == "kanban.create_task":
        title = _required_string(payload, "title", max_length=200)
        assignee = _required_string(payload, "assignee", max_length=80)
        if not _ASSIGNEE_RE.fullmatch(assignee):
            raise HTTPException(status_code=422, detail="assignee is invalid")
        body = _optional_string(payload, "body", max_length=5000) or ""
        parents = payload.get("parents", [])
        if parents is None:
            parents = []
        if not isinstance(parents, list) or len(parents) > 10:
            raise HTTPException(status_code=422, detail="parents must be a list of up to 10 task ids")
        clean_parents = []
        for parent in parents:
            if not isinstance(parent, str) or not _TASK_ID_RE.fullmatch(parent):
                raise HTTPException(status_code=422, detail="parent task id is invalid")
            clean_parents.append(parent)
        priority = payload.get("priority")
        if priority is not None and (not isinstance(priority, int) or priority < 0 or priority > 100):
            raise HTTPException(status_code=422, detail="priority must be an integer from 0 to 100")
        return {"title": title, "assignee": assignee, "body": body, "parents": clean_parents, "priority": priority}
    raise HTTPException(status_code=403, detail="capability not executable")


def _required_string(payload: dict[str, Any], key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{key} is required")
    value = value.strip()
    if len(value) > max_length:
        raise HTTPException(status_code=422, detail=f"{key} is too long")
    return value


def _optional_string(payload: dict[str, Any], key: str, *, max_length: int) -> Optional[str]:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{key} must be a string")
    value = value.strip()
    if len(value) > max_length:
        raise HTTPException(status_code=422, detail=f"{key} is too long")
    return value


def _required_task_id(payload: dict[str, Any], key: str) -> str:
    value = _required_string(payload, key, max_length=80)
    if not _TASK_ID_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{key} is invalid")
    return value


def _artifact_describe(store: ArtifactStore, artifact: Artifact, payload: dict[str, Any], executor: KanbanExecutor) -> dict[str, Any]:
    updated = store.update_metadata(artifact.slug, title=payload.get("title"), description=payload.get("description"))
    return {"slug": updated.slug, "title": updated.title, "description": updated.description}


def _artifact_archive(store: ArtifactStore, artifact: Artifact, payload: dict[str, Any], executor: KanbanExecutor) -> dict[str, Any]:
    updated = store.archive(artifact.slug, reason=payload["reason"])
    return {"slug": updated.slug, "status": updated.status, "archive_reason": updated.archive_reason}


def _kanban_comment(store: ArtifactStore, artifact: Artifact, payload: dict[str, Any], executor: KanbanExecutor) -> dict[str, Any]:
    return executor.comment(payload["task_id"], payload["body"])


def _kanban_create_task(store: ArtifactStore, artifact: Artifact, payload: dict[str, Any], executor: KanbanExecutor) -> dict[str, Any]:
    return executor.create_task(
        title=payload["title"],
        assignee=payload["assignee"],
        body=payload["body"],
        parents=payload["parents"],
        priority=payload["priority"],
    )


def _approval_placeholder(store: ArtifactStore, artifact: Artifact, payload: dict[str, Any], executor: KanbanExecutor) -> dict[str, Any]:
    raise HTTPException(status_code=403, detail="approval required before this action can run")


def _result_summary(result: dict[str, Any]) -> str:
    if "task_id" in result:
        return f"task_id={result['task_id']}"
    if "slug" in result:
        return f"slug={result['slug']}"
    if "stdout" in result and result["stdout"]:
        return str(result["stdout"])[:200]
    return "ok"


CAPABILITY_REGISTRY: dict[str, Capability] = {
    "artifact.describe": Capability(
        name="artifact.describe",
        description="Update this artifact's title and/or searchable description.",
        schema={"type": "object", "properties": {"title": {"type": "string", "maxLength": 200}, "description": {"type": "string", "maxLength": 2000}}},
        handler=_artifact_describe,
    ),
    "artifact.archive": Capability(
        name="artifact.archive",
        description="Archive this artifact so it is hidden from the default home page.",
        schema={"type": "object", "properties": {"reason": {"type": "string", "maxLength": 500}}},
        handler=_artifact_archive,
    ),
    "artifact.state": Capability(
        name="artifact.state",
        description="Read and write a small JSON state document for this artifact through /{slug}/_state/{key}.",
        schema={"type": "object", "required": ["snapshot"], "properties": {"snapshot": {"type": "object"}, "client_id": {"type": "string"}, "expected_version": {"type": "integer"}}},
        handler=_approval_placeholder,
        executes_via="artifactd state sidecar",
    ),
    "kanban.comment": Capability(
        name="kanban.comment",
        description="Append a comment to an existing Hermes Kanban task.",
        schema={"type": "object", "required": ["task_id", "body"]},
        handler=_kanban_comment,
        provider="hermes-profile",
        executes_via="Hermes profile tool/plugin bridge",
    ),
    "kanban.create_task": Capability(
        name="kanban.create_task",
        description="Create a new Hermes Kanban task with an explicit assignee.",
        schema={"type": "object", "required": ["title", "assignee"]},
        handler=_kanban_create_task,
        provider="hermes-profile",
        executes_via="Hermes profile tool/plugin bridge",
    ),
    "draft.email": Capability(
        name="draft.email",
        description="Approval-gated placeholder for future external email actions.",
        schema={"type": "object"},
        handler=_approval_placeholder,
        approval_required=True,
    ),
}
