from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from .store import ArtifactStore
from .workspaces import resolve_workspace_home


def _profile_from_args(args: Optional[dict[str, Any]] = None) -> str:
    args = args or {}
    return str(args.get("profile") or "palmer")


def _workspace_home(args: Optional[dict[str, Any]] = None) -> Path:
    args = args or {}
    profile = _profile_from_args(args)
    hermes_root = Path(args["hermes_root"]) if args.get("hermes_root") else None
    profile_home = Path(args["profile_home"]) if args.get("profile_home") else None
    return resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _tool_status(args: Optional[dict[str, Any]] = None, **kwargs) -> str:
    try:
        home = _workspace_home(args)
        store = ArtifactStore(home)
        return _json(
            {
                "success": True,
                "profile": _profile_from_args(args),
                "workspace_home": str(home),
                "workspace_password_configured": store.workspace_password_configured(),
                "active_things": len(list(store.list(status="active"))),
                "pinned_things": len(store.list_workspace_things(bucket="pinned")),
                "requires_action_things": len(store.list_workspace_things(bucket="requires-action")),
            }
        )
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})


def _tool_smoke(args: Optional[dict[str, Any]] = None, **kwargs) -> str:
    args = args or {}
    try:
        home = _workspace_home(args)
        store = ArtifactStore(home)
        if args.get("password"):
            store.set_workspace_password(str(args["password"]))
        smoke_dir = home / ".smoke-source"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        (smoke_dir / "index.html").write_text(
            "<!doctype html><html><body><h1>Hermes Workspaces smoke</h1></body></html>",
            encoding="utf-8",
        )
        thing = store.register_thing(
            smoke_dir,
            slug="hermes-workspaces-smoke",
            title="Hermes Workspaces smoke",
            description="Protected smoke Thing for profile-scoped Hermes Workspaces.",
            capabilities=["artifact.describe"],
        )
        return _json({"success": True, "workspace_home": str(home), "slug": thing.slug, "auth_mode": thing.auth_mode})
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})


def _tool_register_thing(args: Optional[dict[str, Any]] = None, **kwargs) -> str:
    args = args or {}
    try:
        source = args.get("source")
        slug = args.get("slug")
        if not source or not slug:
            return _json({"success": False, "error": "source and slug are required"})
        home = _workspace_home(args)
        thing = ArtifactStore(home).register_thing(
            Path(str(source)),
            slug=str(slug),
            title=args.get("title"),
            description=args.get("description"),
            capabilities=args.get("capabilities") or [],
            requires_action=bool(args.get("requires_action", False)),
            pinned=bool(args.get("pinned", False)),
        )
        return _json({"success": True, "workspace_home": str(home), "slug": thing.slug, "auth_mode": thing.auth_mode})
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})


def _slash_workspaces(raw_args: str = "") -> str:
    payload = json.loads(_tool_status({}))
    if not payload.get("success"):
        return "Workspaces status failed: " + payload.get("error", "unknown error")
    return "Workspaces status: " + ", ".join(f"{k}={v}" for k, v in payload.items() if k != "success")


def _register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="workspaces_command")
    subs.add_parser("status", help="Show Workspaces status")
    smoke = subs.add_parser("smoke", help="Create a protected smoke Thing")
    smoke.add_argument("--profile", default="palmer")
    smoke.add_argument("--password", default=None)
    reg = subs.add_parser("register", help="Register a generated Thing")
    reg.add_argument("source")
    reg.add_argument("--profile", default="palmer")
    reg.add_argument("--slug", required=True)
    reg.add_argument("--title")
    reg.add_argument("--description")
    reg.add_argument("--capability", action="append", dest="capabilities", default=[])
    reg.add_argument("--requires-action", action="store_true")
    reg.add_argument("--pinned", action="store_true")
    subparser.set_defaults(func=_cli_command)


def _cli_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "workspaces_command", None) or "status"
    if sub == "status":
        result = json.loads(_tool_status({"profile": getattr(args, "profile", "palmer")}))
    elif sub == "smoke":
        result = json.loads(_tool_smoke({"profile": args.profile, "password": args.password}))
    elif sub == "register":
        result = json.loads(
            _tool_register_thing(
                {
                    "profile": args.profile,
                    "source": args.source,
                    "slug": args.slug,
                    "title": args.title,
                    "description": args.description,
                    "capabilities": args.capabilities,
                    "requires_action": args.requires_action,
                    "pinned": args.pinned,
                }
            )
        )
    else:
        print("usage: hermes workspaces {status,smoke,register}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


_STATUS_SCHEMA = {
    "name": "workspaces_status",
    "description": "Show Hermes Workspaces status for a profile.",
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {"type": "string"},
            "hermes_root": {"type": "string"},
            "profile_home": {"type": "string"},
        },
    },
}
_SMOKE_SCHEMA = {
    "name": "workspaces_smoke",
    "description": "Create a protected smoke Thing in a profile workspace.",
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {"type": "string"},
            "password": {"type": "string"},
            "hermes_root": {"type": "string"},
            "profile_home": {"type": "string"},
        },
    },
}
_REGISTER_SCHEMA = {
    "name": "workspaces_register_thing",
    "description": "Register an existing HTML file/directory as a profile-owned generated Thing.",
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {"type": "string"},
            "source": {"type": "string"},
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "requires_action": {"type": "boolean"},
            "pinned": {"type": "boolean"},
            "hermes_root": {"type": "string"},
            "profile_home": {"type": "string"},
        },
        "required": ["source", "slug"],
    },
}


def register(ctx) -> None:
    ctx.register_tool(name="workspaces_status", toolset="workspaces", schema=_STATUS_SCHEMA, handler=_tool_status, emoji="🏠")
    ctx.register_tool(name="workspaces_smoke", toolset="workspaces", schema=_SMOKE_SCHEMA, handler=_tool_smoke, emoji="💨")
    ctx.register_tool(name="workspaces_register_thing", toolset="workspaces", schema=_REGISTER_SCHEMA, handler=_tool_register_thing, emoji="🧩")
    ctx.register_command(name="workspaces", handler=_slash_workspaces, description="Show Hermes Workspaces status", args_hint="status")
    ctx.register_cli_command(name="workspaces", help="Hermes Workspaces sidecar", setup_fn=_register_cli, handler_fn=_cli_command, description="Control Workspaces without Hermes core patches.")
