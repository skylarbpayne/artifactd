from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import Optional

from .workspaces import resolve_profile_home, resolve_workspace_home

PLUGIN_NAME = "artifactd_workspaces"


def install_hermes_plugin(
    *,
    profile: str,
    hermes_root: Optional[Path] = None,
    profile_home: Optional[Path] = None,
    runtime_path: Optional[Path] = None,
    port: int = 8787,
    public_base_url: Optional[str] = None,
    enable: bool = False,
    force: bool = False,
) -> Path:
    """Install the Workspaces Hermes plugin into one profile, without touching Hermes core.

    The installed plugin is a profile-local directory plugin under
    ``$HERMES_HOME/plugins/artifactd_workspaces``. It shells out to the chosen
    artifactd runtime, so Hermes does not need Workspaces hardcoded into core.
    """

    resolved_profile_home = resolve_profile_home(
        profile, hermes_root=hermes_root, profile_home=profile_home
    )
    resolved_workspace_home = resolve_workspace_home(
        profile, hermes_root=hermes_root, profile_home=profile_home
    )
    runtime = _resolve_runtime_path(runtime_path)
    plugin_dir = resolved_profile_home / "plugins" / PLUGIN_NAME
    if plugin_dir.exists() and not force:
        # Overwrite the generated files idempotently; refuse only if the target
        # does not look like our plugin.
        marker = plugin_dir / "plugin.yaml"
        if marker.exists() and PLUGIN_NAME not in marker.read_text(encoding="utf-8", errors="replace"):
            raise FileExistsError(f"{plugin_dir} exists and is not {PLUGIN_NAME}; pass --force to replace")
    if plugin_dir.exists() and force:
        shutil.rmtree(plugin_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "profile": profile,
        "profile_home": str(resolved_profile_home),
        "workspace_home": str(resolved_workspace_home),
        "runtime_path": str(runtime),
        "port": int(port),
        "public_base_url": (public_base_url or "").rstrip("/") or None,
    }
    (plugin_dir / "plugin_config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (plugin_dir / "plugin.yaml").write_text(_plugin_yaml(), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(_directory_plugin_source(), encoding="utf-8")
    (plugin_dir / "README.md").write_text(_plugin_readme(), encoding="utf-8")

    if enable:
        enable_profile_plugin(resolved_profile_home, PLUGIN_NAME)
    return plugin_dir


def enable_profile_plugin(profile_home: Path, plugin_name: str = PLUGIN_NAME) -> None:
    """Add the plugin to ``plugins.enabled`` in profile config.yaml."""

    profile_home.mkdir(parents=True, exist_ok=True)
    config_path = profile_home / "config.yaml"
    data = {}
    if config_path.exists():
        try:
            import yaml

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = _minimal_config_parse(config_path.read_text(encoding="utf-8"))
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
    if plugin_name not in enabled:
        enabled.append(plugin_name)
    plugins["enabled"] = enabled
    try:
        import yaml

        config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    except Exception:
        config_path.write_text(_minimal_config_dump(data), encoding="utf-8")


def _resolve_runtime_path(runtime_path: Optional[Path]) -> Path:
    if runtime_path is not None:
        return Path(runtime_path).expanduser().resolve()
    env_runtime = os.environ.get("ARTIFACTD_BIN")
    if env_runtime:
        return Path(env_runtime).expanduser().resolve()
    discovered = shutil.which("artifactd")
    if discovered:
        return Path(discovered).resolve()
    return Path(sys.executable).resolve().parent / "artifactd"


def _version() -> str:
    try:
        return metadata.version("artifactd")
    except Exception:
        return "0.1.0"


def _plugin_yaml() -> str:
    return f"""name: {PLUGIN_NAME}
version: {_version()}
description: "Hermes Workspaces sidecar plugin for profile-owned generated Things."
author: Skylar Payne / Palmer
kind: standalone
provides_tools:
  - workspaces_status
  - workspaces_home
  - workspaces_smoke
  - workspaces_register_thing
"""


def _plugin_readme() -> str:
    return """# artifactd Workspaces Hermes plugin

Profile-local Hermes plugin installed by `artifactd workspaces install-plugin`.

This plugin intentionally lives under the profile's `plugins/` directory and
shells out to the configured artifactd runtime. It does not patch Hermes core.
Restart the Hermes gateway/session after installing or enabling it.
"""


def _directory_plugin_source() -> str:
    # Self-contained on purpose: profile-local Hermes plugin should load even
    # when artifactd is not importable in Hermes' Python environment.
    return r'''from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

PLUGIN_NAME = "artifactd_workspaces"


def _config() -> dict:
    path = Path(__file__).with_name("plugin_config.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _run_artifactd(args: list[str]) -> dict:
    cfg = _config()
    runtime = cfg["runtime_path"]
    proc = subprocess.run([runtime, *args], text=True, capture_output=True, check=False)
    parsed = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key.strip()] = value.strip()
    return {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "data": parsed,
    }


def _base_workspace_args(command: str) -> list[str]:
    cfg = _config()
    return [
        "workspaces",
        command,
        "--profile",
        cfg["profile"],
        "--profile-home",
        cfg["profile_home"],
    ]


def _json(result: dict) -> str:
    return json.dumps(result, sort_keys=True)


def _tool_status(args=None, **kwargs) -> str:
    return _json(_run_artifactd(_base_workspace_args("status")))


def _tool_home(args=None, **kwargs) -> str:
    result = _run_artifactd(_base_workspace_args("home"))
    if result["success"]:
        try:
            result["home"] = json.loads(result.get("stdout") or "{}")
        except Exception as exc:
            result["success"] = False
            result["error"] = f"home JSON parse failed: {exc}"
    return _json(result)


def _tool_smoke(args=None, **kwargs) -> str:
    args = args or {}
    cmd = _base_workspace_args("smoke")
    password = args.get("password")
    if password:
        cmd.extend(["--password", str(password)])
    return _json(_run_artifactd(cmd))


def _tool_register_thing(args=None, **kwargs) -> str:
    args = args or {}
    source = args.get("source")
    slug = args.get("slug")
    if not source or not slug:
        return _json({"success": False, "error": "source and slug are required"})
    cmd = _base_workspace_args("register")
    cmd.insert(2, str(source))
    cmd.extend(["--slug", str(slug)])
    for key, flag in (("title", "--title"), ("description", "--description")):
        if args.get(key):
            cmd.extend([flag, str(args[key])])
    for cap in args.get("capabilities") or []:
        cmd.extend(["--capability", str(cap)])
    for tag in args.get("tags") or []:
        cmd.extend(["--tag", str(tag)])
    if args.get("requires_action"):
        cmd.append("--requires-action")
    if args.get("pinned"):
        cmd.append("--pinned")
    return _json(_run_artifactd(cmd))


def _slash_workspaces(raw_args: str = "") -> str:
    parts = (raw_args or "status").strip().split()
    sub = parts[0] if parts else "status"
    if sub != "status":
        return "Workspaces plugin supports `/workspaces status` for now."
    result = _run_artifactd(_base_workspace_args("status"))
    if not result["success"]:
        return "Workspaces status failed:\n" + result.get("stderr", "")
    return "Workspaces status:\n" + result.get("stdout", "")


def _register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="workspaces_command")
    subs.add_parser("status", help="Show Workspaces status for this profile")
    smoke = subs.add_parser("smoke", help="Create a protected smoke Thing")
    smoke.add_argument("--password", default=None)
    reg = subs.add_parser("register", help="Register a generated Thing")
    reg.add_argument("source")
    reg.add_argument("--slug", required=True)
    reg.add_argument("--title")
    reg.add_argument("--description")
    reg.add_argument("--capability", action="append", dest="capabilities", default=[])
    reg.add_argument("--tag", action="append", dest="tags", default=[])
    reg.add_argument("--requires-action", action="store_true")
    reg.add_argument("--pinned", action="store_true")
    subparser.set_defaults(func=_cli_command)


def _cli_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "workspaces_command", None) or "status"
    if sub == "status":
        result = json.loads(_tool_status({}))
    elif sub == "smoke":
        result = json.loads(_tool_smoke({"password": getattr(args, "password", None)}))
    elif sub == "register":
        result = json.loads(_tool_register_thing({
            "source": args.source,
            "slug": args.slug,
            "title": args.title,
            "description": args.description,
            "capabilities": args.capabilities,
            "tags": args.tags,
            "requires_action": args.requires_action,
            "pinned": args.pinned,
        }))
    else:
        print("usage: hermes workspaces {status,smoke,register}")
        return 2
    print(result.get("stdout", ""), end="")
    if result.get("stderr"):
        print(result["stderr"], end="")
    return 0 if result.get("success") else int(result.get("exit_code") or 1)


_STATUS_SCHEMA = {
    "name": "workspaces_status",
    "description": "Show Hermes Workspaces status for the owning profile.",
    "parameters": {"type": "object", "properties": {}},
}
_HOME_SCHEMA = {
    "name": "workspaces_home",
    "description": "Return Hermes Home/Things dashboard JSON for the owning profile.",
    "parameters": {"type": "object", "properties": {}},
}
_SMOKE_SCHEMA = {
    "name": "workspaces_smoke",
    "description": "Create a protected smoke Thing in the owning profile workspace.",
    "parameters": {
        "type": "object",
        "properties": {"password": {"type": "string", "description": "Optional dev-only workspace smoke password."}},
    },
}
_REGISTER_SCHEMA = {
    "name": "workspaces_register_thing",
    "description": "Register an existing HTML file/directory as a profile-owned generated Thing.",
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "requires_action": {"type": "boolean"},
            "pinned": {"type": "boolean"},
        },
        "required": ["source", "slug"],
    },
}


def register(ctx) -> None:
    ctx.register_tool(name="workspaces_status", toolset="workspaces", schema=_STATUS_SCHEMA, handler=_tool_status, emoji="🏠")
    ctx.register_tool(name="workspaces_home", toolset="workspaces", schema=_HOME_SCHEMA, handler=_tool_home, emoji="🧭")
    ctx.register_tool(name="workspaces_smoke", toolset="workspaces", schema=_SMOKE_SCHEMA, handler=_tool_smoke, emoji="💨")
    ctx.register_tool(name="workspaces_register_thing", toolset="workspaces", schema=_REGISTER_SCHEMA, handler=_tool_register_thing, emoji="🧩")
    ctx.register_command(name="workspaces", handler=_slash_workspaces, description="Show Hermes Workspaces status", args_hint="status")
    ctx.register_cli_command(name="workspaces", help="Hermes Workspaces sidecar", setup_fn=_register_cli, handler_fn=_cli_command, description="Control the artifactd Workspaces sidecar without Hermes core patches.")
'''


def _minimal_config_parse(text: str) -> dict:
    enabled: list[str] = []
    in_plugins = False
    in_enabled = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and stripped == "plugins:":
            in_plugins = True
            in_enabled = False
            continue
        if in_plugins and line.startswith(" ") and stripped == "enabled:":
            in_enabled = True
            continue
        if in_plugins and in_enabled and stripped.startswith("-"):
            enabled.append(stripped[1:].strip().strip('"\''))
        elif not line.startswith(" "):
            in_plugins = False
            in_enabled = False
    return {"plugins": {"enabled": enabled}}


def _minimal_config_dump(data: dict) -> str:
    enabled = data.get("plugins", {}).get("enabled", [])
    lines = ["plugins:", "  enabled:"]
    for item in enabled:
        lines.append(f"  - {item}")
    return "\n".join(lines) + "\n"
