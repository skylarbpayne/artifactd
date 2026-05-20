import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from artifactd.actions import KanbanExecutor
from artifactd.cli import app
from artifactd.server import create_app
from artifactd.store import ArtifactStore
from artifactd.workspaces import resolve_profile_home, resolve_workspace_home


def test_profile_home_and_workspace_home_are_profile_scoped(tmp_path: Path, monkeypatch):
    hermes_root = tmp_path / ".hermes"
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert resolve_profile_home("palmer", hermes_root=hermes_root) == hermes_root / "profiles" / "palmer"
    assert resolve_workspace_home("palmer", hermes_root=hermes_root) == hermes_root / "profiles" / "palmer" / "workspaces"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    assert resolve_profile_home("palmer") == tmp_path / "profile-home"
    assert resolve_workspace_home("palmer") == tmp_path / "profile-home" / "workspaces"


@pytest.mark.parametrize("profile", ["../echo", "echo/workspaces", "/tmp/echo", "", "echo..prod"])
def test_profile_home_rejects_path_like_profile_names(tmp_path: Path, profile: str):
    with pytest.raises(ValueError):
        resolve_workspace_home(profile, hermes_root=tmp_path / ".hermes")


def test_register_generated_thing_defaults_to_profile_auth_and_workspace_buckets(tmp_path: Path):
    source = tmp_path / "day.html"
    source.write_text("<h1>Day plan</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")

    thing = store.register_thing(
        source,
        slug="day-plan",
        title="Day Plan",
        description="Generated daily cockpit",
        capabilities=["kanban.comment"],
        requires_action=True,
        pinned=True,
    )

    assert thing.slug == "day-plan"
    assert thing.auth_mode == "profile"
    assert not thing.has_password
    assert thing.requires_action is True
    assert thing.pinned is True
    assert thing.capabilities == ("kanban.comment",)
    assert "profile-secret" not in store.get_setting("workspace_password_hash")
    assert [item.slug for item in store.list_workspace_things(bucket="requires-action")] == ["day-plan"]
    assert [item.slug for item in store.list_workspace_things(bucket="pinned")] == ["day-plan"]
    assert [item.slug for item in store.list_workspace_things(bucket="recent")] == ["day-plan"]


def test_workspace_share_override_token_unlocks_one_profile_protected_thing(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="thing", title="Thing")

    token = store.create_share_override("thing", token="share-me")
    thing = store.get("thing")

    assert token == "share-me"
    assert thing.share_token_hash
    assert "share-me" not in thing.share_token_hash
    assert store.verify_share_token("thing", "share-me")
    assert not store.verify_share_token("thing", "wrong")


def test_redeploying_existing_public_artifact_with_password_switches_to_custom_auth(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")

    public = store.deploy(source, slug="thing")
    protected = store.deploy(source, slug="thing", password="per-thing-secret")

    assert public.auth_mode == "public"
    assert protected.auth_mode == "custom"
    assert protected.has_password


def test_workspace_home_and_profile_session_unlock_generated_things(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Protected thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(
        source,
        slug="protected-thing",
        title="Protected Thing",
        description="Needs review",
        capabilities=["artifact.describe"],
        requires_action=True,
    )
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    locked_home = client.get("/")
    locked_thing = client.get("/protected-thing")
    accepted = client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    unlocked_home = client.get("/")
    unlocked_thing = client.get("/protected-thing")

    assert locked_home.status_code == 401
    assert locked_thing.status_code == 401
    assert accepted.status_code == 303
    assert unlocked_home.status_code == 200
    for label in ["Hermes Home", "Protected Thing", "Open", "Share", "Update", "Pin", "Archive", "Requires action"]:
        assert label in unlocked_home.text
    assert unlocked_thing.status_code == 200
    assert "Protected thing" in unlocked_thing.text


def test_workspace_share_token_serves_without_profile_session(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Shared thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="shared-thing", title="Shared Thing")
    store.create_share_override("shared-thing", token="review-token")
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    locked = client.get("/shared-thing")
    shared = client.get("/shared-thing?share=review-token")

    assert locked.status_code == 401
    assert shared.status_code == 200
    assert "Shared thing" in shared.text


def test_migrated_password_hash_still_requires_artifact_password(tmp_path: Path):
    source = tmp_path / "legacy.html"
    source.write_text("<h1>Legacy protected</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.deploy(source, slug="legacy-secret", password="opensesame")
    with store._connect() as con:
        con.execute("UPDATE artifacts SET auth_mode = 'public' WHERE slug = 'legacy-secret'")
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    locked = client.get("/legacy-secret")
    accepted = client.post("/legacy-secret/login", data={"password": "opensesame"}, follow_redirects=False)
    unlocked = client.get("/legacy-secret")

    assert store.get("legacy-secret").has_password
    assert store.get("legacy-secret").auth_mode == "public"
    assert locked.status_code == 401
    assert accepted.status_code == 303
    assert unlocked.status_code == 200
    assert "Legacy protected" in unlocked.text


def test_capability_executor_uses_owning_profile_not_hardcoded_palmer(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="commented", stderr="")

    monkeypatch.setattr("artifactd.actions.subprocess.run", fake_run)

    result = KanbanExecutor(profile="echo").comment("t_abc123", "hi")

    assert result["stdout"] == "commented"
    assert calls[0][:4] == ["hermes", "-p", "echo", "kanban"]


def test_capability_executor_infers_profile_from_environment(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="commented", stderr="")

    monkeypatch.setattr("artifactd.actions.subprocess.run", fake_run)
    monkeypatch.delenv("ARTIFACTD_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "echo"))

    KanbanExecutor().comment("t_abc123", "hi")

    assert calls[0][:4] == ["hermes", "-p", "echo", "kanban"]


def test_workspaces_smoke_command_is_profile_aware_and_creates_protected_thing(tmp_path: Path):
    runner = CliRunner()
    hermes_root = tmp_path / ".hermes"

    result = runner.invoke(
        app,
        [
            "workspaces",
            "smoke",
            "--profile",
            "echo",
            "--hermes-root",
            str(hermes_root),
            "--password",
            "profile-secret",
        ],
    )

    assert result.exit_code == 0
    workspace_home = hermes_root / "profiles" / "echo" / "workspaces"
    assert f"profile=echo" in result.output
    assert f"workspace_home={workspace_home}" in result.output
    assert "created hermes-workspaces-smoke" in result.output
    smoke = ArtifactStore(workspace_home).get("hermes-workspaces-smoke")
    assert smoke.auth_mode == "profile"
    assert smoke.requires_action is False
    assert smoke.capabilities == ("artifact.describe",)


def test_workspace_registry_endpoint_lists_home_buckets_after_profile_login(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="needs-review", title="Needs Review", requires_action=True, pinned=True)
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    locked = client.get("/_workspace/things")
    client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    registry = client.get("/_workspace/things")
    pinned_page = client.get("/?bucket=pinned")

    assert locked.status_code == 401
    assert registry.status_code == 200
    payload = registry.json()
    assert payload["counts"] == {"active": 1, "pinned": 1, "recent": 1, "requires-action": 1, "archived": 0}
    assert payload["buckets"]["requires_action"][0]["slug"] == "needs-review"
    assert payload["buckets"]["pinned"][0]["protected"] is True
    assert pinned_page.status_code == 200
    assert "Pinned <span>1</span>" in pinned_page.text
    assert "Needs Review" in pinned_page.text


def test_workspace_home_dashboard_endpoint_exposes_things_actions_and_bridge_metadata(tmp_path: Path):
    source = tmp_path / "day.html"
    source.write_text("<h1>Day</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(
        source,
        slug="day-plan",
        title="Day Plan",
        description="Today cockpit",
        capabilities=["artifact.describe", "kanban.comment"],
        requires_action=True,
        pinned=True,
    )
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret", profile="echo"))

    locked = client.get("/_workspace/home")
    client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    response = client.get("/_workspace/home")
    home_page = client.get("/")

    assert locked.status_code == 401
    assert response.status_code == 200
    assert home_page.status_code == 200
    assert "Hermes profile bridge" in home_page.text
    assert "profile=echo" in home_page.text
    payload = response.json()
    assert payload["kind"] == "HermesWorkspaceHome"
    assert payload["profile"] == "echo"
    assert payload["language"] == {"home": "Home", "thing": "Thing", "things": "Things"}
    assert payload["counts"]["requires-action"] == 1
    thing = payload["buckets"]["pinned"][0]
    assert thing["slug"] == "day-plan"
    assert thing["open_url"] == "/day-plan"
    assert thing["actions_url"] == "/day-plan/_actions"
    assert thing["workspace_actions"]["pin"]["method"] == "POST"
    assert thing["workspace_actions"]["archive"]["approval_required"] is False
    assert thing["capability_bridge"] == {
        "provider": "hermes-profile",
        "profile": "echo",
        "audit_actor": "hermes-profile:echo",
    }
    assert thing["capabilities"]["kanban.comment"]["provider"] == "hermes-profile"
    assert thing["capabilities"]["kanban.comment"]["approval_required"] is False


def test_action_manifest_and_audit_use_hermes_profile_bridge_actor(tmp_path: Path):
    class FakeHermesBridge:
        profile = "echo"
        actor = "hermes-profile:echo"

        def comment(self, task_id: str, body: str):
            return {"task_id": task_id, "body": body, "profile": self.profile}

        def create_task(self, **kwargs):  # pragma: no cover - not used here
            return {"profile": self.profile, **kwargs}

    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="thing", title="Thing", capabilities=["kanban.comment"])
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret", kanban_executor=FakeHermesBridge(), profile="echo"))

    client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    manifest = client.get("/thing/_actions")
    csrf = manifest.json()["csrf_token"]
    action = client.post(
        "/thing/_actions/kanban.comment",
        json={"task_id": "t_abc123", "body": "from thing", "_csrf": csrf},
    )

    assert manifest.status_code == 200
    capability = manifest.json()["capabilities"][0]
    assert capability["provider"] == "hermes-profile"
    assert capability["profile"] == "echo"
    assert capability["executes_via"] == "Hermes profile tool/plugin bridge"
    assert action.status_code == 200
    assert action.json()["result"]["profile"] == "echo"
    audits = store.list_action_audit("thing")
    assert audits[-1].actor == "hermes-profile:echo"
    assert audits[-1].capability == "kanban.comment"


def test_workspace_home_forms_pin_share_requires_action_and_archive_with_csrf(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="thing", title="Thing")
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    no_csrf = client.post("/_workspace/things/thing/pin", data={"pinned": "true"}, follow_redirects=False)
    client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    home = client.get("/")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', home.text).group(1)
    pinned = client.post("/_workspace/things/thing/pin", data={"csrf_token": csrf, "pinned": "true"}, follow_redirects=False)
    actioned = client.post("/_workspace/things/thing/requires-action", data={"csrf_token": csrf, "requires_action": "true"}, follow_redirects=False)
    share = client.post("/_workspace/things/thing/share", data={"csrf_token": csrf}, follow_redirects=False)
    token_match = re.search(r'/thing\?share=([^"<]+)', share.text)
    archived = client.post("/_workspace/things/thing/archive", data={"csrf_token": csrf}, follow_redirects=False)

    assert no_csrf.status_code == 401
    assert pinned.status_code == 303
    assert actioned.status_code == 303
    assert share.status_code == 200
    assert "Share link created" in share.text
    assert token_match
    assert client.get(f"/thing?share={token_match.group(1)}").status_code == 200
    thing = store.get("thing")
    assert thing.pinned is True
    assert thing.requires_action is True
    assert archived.status_code == 303
    assert store.get("thing").status == "archived"
    audits = store.list_action_audit("thing")
    assert [audit.capability for audit in audits] == ["workspace.pin", "workspace.requires_action", "workspace.share", "workspace.archive"]
