from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from artifactd.cli import app
from artifactd.store import ArtifactStore


PLUGIN_NAME = "artifactd_workspaces"


def test_workspaces_register_command_creates_profile_auth_thing(tmp_path: Path):
    runner = CliRunner()
    hermes_root = tmp_path / ".hermes"
    source = tmp_path / "daily.html"
    source.write_text("<h1>Daily cockpit</h1>", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "workspaces",
            "register",
            str(source),
            "--profile",
            "echo",
            "--hermes-root",
            str(hermes_root),
            "--slug",
            "daily-cockpit",
            "--title",
            "Daily cockpit",
            "--description",
            "Profile-owned generated Thing",
            "--capability",
            "kanban.comment",
            "--requires-action",
            "--pinned",
        ],
    )

    assert result.exit_code == 0, result.output
    workspace_home = hermes_root / "profiles" / "echo" / "workspaces"
    thing = ArtifactStore(workspace_home).get("daily-cockpit")
    assert thing.auth_mode == "profile"
    assert thing.title == "Daily cockpit"
    assert thing.description == "Profile-owned generated Thing"
    assert thing.capabilities == ("kanban.comment",)
    assert thing.requires_action is True
    assert thing.pinned is True
    assert f"workspace_home={workspace_home}" in result.output
    assert "registered daily-cockpit" in result.output


def test_workspaces_install_plugin_writes_profile_plugin_and_enables_config(tmp_path: Path):
    runner = CliRunner()
    hermes_root = tmp_path / ".hermes"
    runtime = tmp_path / "bin" / "artifactd"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime.chmod(0o755)

    result = runner.invoke(
        app,
        [
            "workspaces",
            "install-plugin",
            "--profile",
            "palmer",
            "--hermes-root",
            str(hermes_root),
            "--runtime-path",
            str(runtime),
            "--port",
            "18787",
            "--public-base-url",
            "https://artifacts.example.com",
            "--enable",
        ],
    )

    assert result.exit_code == 0, result.output
    profile_home = hermes_root / "profiles" / "palmer"
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    assert (plugin_dir / "plugin.yaml").is_file()
    assert (plugin_dir / "__init__.py").is_file()
    plugin_config = json.loads((plugin_dir / "plugin_config.json").read_text(encoding="utf-8"))
    assert plugin_config == {
        "profile": "palmer",
        "profile_home": str(profile_home),
        "workspace_home": str(profile_home / "workspaces"),
        "runtime_path": str(runtime),
        "port": 18787,
        "public_base_url": "https://artifacts.example.com",
    }
    config_text = (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:" in config_text
    assert "artifactd_workspaces" in config_text
    assert "installed_plugin=true" in result.output
    assert f"plugin_dir={plugin_dir}" in result.output


def test_installed_directory_plugin_registers_workspace_tools_without_core_changes(tmp_path: Path):
    runner = CliRunner()
    hermes_root = tmp_path / ".hermes"
    runtime = Path(shutil.which("artifactd") or sys.executable)
    result = runner.invoke(
        app,
        [
            "workspaces",
            "install-plugin",
            "--profile",
            "echo",
            "--hermes-root",
            str(hermes_root),
            "--runtime-path",
            str(runtime),
            "--enable",
        ],
    )
    assert result.exit_code == 0, result.output
    plugin_init = hermes_root / "profiles" / "echo" / "plugins" / PLUGIN_NAME / "__init__.py"

    calls = {"tools": [], "commands": [], "cli": []}

    class FakeCtx:
        def register_tool(self, **kwargs):
            calls["tools"].append(kwargs)

        def register_command(self, **kwargs):
            calls["commands"].append(kwargs)

        def register_cli_command(self, **kwargs):
            calls["cli"].append(kwargs)

    module_name = "test_artifactd_workspaces_plugin"
    spec = importlib.util.spec_from_file_location(module_name, plugin_init)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        module.register(FakeCtx())
    finally:
        sys.modules.pop(module_name, None)

    tool_names = {item["name"] for item in calls["tools"]}
    assert {"workspaces_status", "workspaces_home", "workspaces_smoke", "workspaces_register_thing"}.issubset(tool_names)
    home_tool = next(item for item in calls["tools"] if item["name"] == "workspaces_home")
    source = hermes_root / "profile-home.html"
    source.write_text("<h1>Home thing</h1>", encoding="utf-8")
    ArtifactStore(hermes_root / "profiles" / "echo" / "workspaces").register_thing(source, slug="home-thing", title="Home Thing")
    home_payload = json.loads(home_tool["handler"]({"profile": "echo", "hermes_root": str(hermes_root)}))
    assert home_payload["success"] is True
    assert home_payload["home"]["kind"] == "HermesWorkspaceHome"
    assert home_payload["home"]["profile"] == "echo"
    assert home_payload["home"]["buckets"]["active"][0]["slug"] == "home-thing"
    assert calls["commands"][0]["name"] == "workspaces"
    assert calls["cli"][0]["name"] == "workspaces"


def test_pyproject_exposes_pip_plugin_entrypoint():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    group = pyproject["project"]["entry-points"]["hermes_agent.plugins"]
    assert group[PLUGIN_NAME] == "artifactd.hermes_plugin"
