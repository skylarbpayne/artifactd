import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from artifactd.actions import KanbanExecutor
from artifactd.security import hash_password
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


def test_workspace_tags_are_normalized_persisted_imported_and_filter_with_and_semantics(tmp_path: Path):
    wedding = tmp_path / "wedding.html"
    wedding.write_text("<h1>Wedding cockpit</h1>", encoding="utf-8")
    htv = tmp_path / "htv.html"
    htv.write_text("<h1>HTV cockpit</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")

    thing = store.register_thing(
        wedding,
        slug="wedding-cockpit",
        title="Wedding Cockpit",
        tags=[" Wedding ", "planning", "PLANNING", "Jacqueline"],
    )
    store.register_thing(htv, slug="htv-cockpit", title="HTV Cockpit", tags=["planning", "htv"])

    assert thing.tags == ("wedding", "planning", "jacqueline")
    assert store.get("wedding-cockpit").tags == ("wedding", "planning", "jacqueline")
    assert [item.slug for item in store.list_workspace_things(bucket="active", tags=["planning", "wedding"])] == ["wedding-cockpit"]
    assert store.tag_facets(bucket="active") == {"htv": 1, "jacqueline": 1, "planning": 2, "wedding": 1}

    imported = ArtifactStore(tmp_path / "imported-workspaces")
    imported.import_legacy_artifacts(store)
    assert imported.get("wedding-cockpit").tags == ("wedding", "planning", "jacqueline")


def test_workspace_share_override_token_unlocks_one_profile_protected_thing_for_one_week_by_default(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="thing", title="Thing")

    token = store.create_share_override("thing", token="share-me", now=1_000)
    thing = store.get("thing")

    assert token == "share-me"
    assert thing.share_token_hash
    assert thing.share_token_expires_at == 1_000 + 7 * 24 * 60 * 60
    assert "share-me" not in thing.share_token_hash
    assert store.verify_share_token("thing", "share-me", now=thing.share_token_expires_at - 1)
    assert not store.verify_share_token("thing", "share-me", now=thing.share_token_expires_at + 1)
    assert not store.verify_share_token("thing", "wrong", now=1_001)


def test_redeploying_existing_public_artifact_with_password_is_rejected(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")

    public = store.deploy(source, slug="thing")

    with pytest.raises(ValueError, match="per-artifact passwords are disabled"):
        store.deploy(source, slug="thing", password="per-thing-secret")
    assert public.auth_mode == "public"
    assert not store.get("thing").has_password


def test_workspace_home_and_profile_session_unlock_generated_things(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Protected thing</h1>", encoding="utf-8")
    profile_source = tmp_path / "profile.html"
    profile_source.write_text("<h1>Second profile protected thing</h1>", encoding="utf-8")
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
    store.deploy(profile_source, slug="profile-thing", title="Profile Thing", auth_mode="profile")
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    locked_home = client.get("/")
    locked_thing = client.get("/protected-thing")
    locked_profile = client.get("/profile-thing")
    login_page = client.get("/profile-thing")
    accepted = client.post("/profile-thing/login", data={"password": "profile-secret"}, follow_redirects=False)
    unlocked_home = client.get("/")
    unlocked_thing = client.get("/protected-thing")
    unlocked_profile = client.get("/profile-thing")

    assert locked_home.status_code == 401
    assert locked_thing.status_code == 401
    assert locked_profile.status_code == 401
    assert "artifactd.masterPassword" in login_page.text
    assert "localStorage" in login_page.text
    assert accepted.status_code == 303
    assert unlocked_home.status_code == 200
    for label in ["Workspace Home", "Protected Thing", "Open", "Share", "Update", "Pin", "Archive", "Requires action"]:
        assert label in unlocked_home.text
    assert "Hermes Home" not in unlocked_home.text
    assert unlocked_thing.status_code == 200
    assert "Protected thing" in unlocked_thing.text
    assert unlocked_profile.status_code == 200
    assert "Second profile protected thing" in unlocked_profile.text


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


def test_migrated_password_hash_requires_workspace_password_not_artifact_password(tmp_path: Path):
    source = tmp_path / "legacy.html"
    source.write_text("<h1>Legacy protected</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("workspace-secret")
    store.deploy(source, slug="legacy-secret")
    with store._connect() as con:
        con.execute("UPDATE artifacts SET password_hash = ?, auth_mode = 'public' WHERE slug = 'legacy-secret'", (hash_password("opensesame"),))
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    locked = client.get("/legacy-secret")
    rejected_artifact_password = client.post("/legacy-secret/login", data={"password": "opensesame"}, follow_redirects=False)
    accepted_workspace_password = client.post("/legacy-secret/login", data={"password": "workspace-secret"}, follow_redirects=False)
    unlocked = client.get("/legacy-secret")

    assert store.get("legacy-secret").has_password
    assert store.get("legacy-secret").auth_mode == "public"
    assert locked.status_code == 401
    assert rejected_artifact_password.status_code == 401
    assert accepted_workspace_password.status_code == 303
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


def test_workspaces_import_legacy_copies_existing_artifacts_idempotently(tmp_path: Path):
    runner = CliRunner()
    legacy_home = tmp_path / "legacy-artifacts"
    hermes_root = tmp_path / ".hermes"
    public_source = tmp_path / "public.html"
    public_source.write_text("<h1>Public Legacy</h1>", encoding="utf-8")
    protected_dir = tmp_path / "protected-dir"
    protected_dir.mkdir()
    (protected_dir / "index.html").write_text("<h1>Protected Legacy</h1>", encoding="utf-8")
    legacy = ArtifactStore(legacy_home)
    legacy.deploy(public_source, slug="public-legacy", title="Public Legacy", description="old public", capabilities=["artifact.describe"], pinned=True)
    legacy.deploy(protected_dir, slug="secret-legacy", title="Secret Legacy", description="old protected", capabilities=["kanban.comment"])
    with legacy._connect() as con:
        con.execute("UPDATE artifacts SET password_hash = ?, auth_mode = 'custom' WHERE slug = 'secret-legacy'", (hash_password("opensesame"),))
    legacy.archive("secret-legacy", reason="old archive", force=True)

    args = [
        "workspaces",
        "import-legacy",
        "--profile",
        "echo",
        "--hermes-root",
        str(hermes_root),
        "--from-home",
        str(legacy_home),
    ]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "imported=2" in first.output
    assert "imported=0" in second.output
    assert "updated=2" in second.output
    workspace_home = hermes_root / "profiles" / "echo" / "workspaces"
    workspace = ArtifactStore(workspace_home)
    public = workspace.get("public-legacy")
    secret = workspace.get("secret-legacy")
    assert public.title == "Public Legacy"
    assert public.description == "old public"
    assert public.auth_mode == "public"
    assert public.pinned is True
    assert public.capabilities == ("artifact.describe",)
    assert (workspace_home / "sites" / "public-legacy" / "index.html").read_text(encoding="utf-8") == "<h1>Public Legacy</h1>"
    assert secret.status == "archived"
    assert secret.archive_reason == "old archive"
    assert secret.auth_mode == "profile"
    assert secret.password_hash is None
    assert (legacy_home / "sites" / "public-legacy" / "index.html").exists()


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


def test_workspace_home_ui_and_json_expose_tag_chips_facets_and_combined_search(tmp_path: Path):
    wedding = tmp_path / "wedding.html"
    wedding.write_text("<h1>Wedding</h1>", encoding="utf-8")
    htv = tmp_path / "htv.html"
    htv.write_text("<h1>HTV</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(wedding, slug="wedding-cockpit", title="Wedding Cockpit", description="Today cockpit", tags=["wedding", "planning"])
    store.register_thing(htv, slug="htv-cockpit", title="HTV Cockpit", description="Today cockpit", tags=["htv", "planning"])
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret", profile="echo"))

    client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    home_page = client.get("/?q=cockpit&tag=wedding&tag=planning")
    home_payload = client.get("/_workspace/home?q=cockpit&tag=wedding&tag=planning").json()
    things_payload = client.get("/_workspace/things?q=cockpit&tag=wedding&tag=planning").json()

    assert home_page.status_code == 200
    assert "Wedding Cockpit" in home_page.text
    assert "HTV Cockpit" not in home_page.text
    assert "tag-chip selected" in home_page.text
    assert "data-tag-filter" in home_page.text
    assert "wedding <span>1</span>" in home_page.text
    assert home_payload["selected_tags"] == ["wedding", "planning"]
    assert home_payload["tag_facets"] == {"htv": 1, "planning": 2, "wedding": 1}
    assert home_payload["buckets"]["active"][0]["tags"] == ["wedding", "planning"]
    assert [item["slug"] for item in things_payload["buckets"]["active"]] == ["wedding-cockpit"]
    assert things_payload["tag_facets"] == {"htv": 1, "planning": 2, "wedding": 1}


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
    assert payload["title"] == "Workspace Home"
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


def test_authenticated_artifact_page_exposes_admin_share_link_without_leaking_to_shared_view(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<!doctype html><html><body><h1>Thing</h1></body></html>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="thing", title="Thing")
    store.create_share_override("thing", token="review-token")
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret"))

    shared = client.get("/thing?share=review-token")
    client.post("/_workspace/login", data={"password": "profile-secret"}, follow_redirects=False)
    admin_view = client.get("/thing")
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', admin_view.text)

    assert shared.status_code == 200
    assert admin_view.status_code == 200
    assert "artifactd-share-toolbar" not in shared.text
    assert "artifactd-share-toolbar" in admin_view.text
    assert "Share link" in admin_view.text
    assert 'action="/_workspace/things/thing/share"' in admin_view.text
    assert csrf_match


def test_workspace_home_forms_pin_share_requires_action_and_archive_with_csrf(tmp_path: Path):
    source = tmp_path / "thing.html"
    source.write_text("<h1>Thing</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "workspaces")
    store.set_workspace_password("profile-secret")
    store.register_thing(source, slug="thing", title="Thing")
    client = TestClient(create_app(tmp_path / "workspaces", cookie_secret="test-secret", public_base_url="https://artifacts.example.com"))

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
    assert "Share link" in home.text
    assert "https://artifacts.example.com/thing?share=" in share.text
    assert "Copy link" in share.text
    assert "Expires in 7 days" in share.text
    assert token_match
    assert client.get(f"/thing?share={token_match.group(1)}").status_code == 200
    thing = store.get("thing")
    assert thing.pinned is True
    assert thing.requires_action is True
    assert archived.status_code == 303
    assert store.get("thing").status == "archived"
    audits = store.list_action_audit("thing")
    assert [audit.capability for audit in audits] == ["workspace.pin", "workspace.requires_action", "workspace.share", "workspace.archive"]
