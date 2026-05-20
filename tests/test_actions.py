from pathlib import Path

from fastapi.testclient import TestClient

from artifactd.server import create_app
from artifactd.store import ArtifactStore


class FakeKanbanExecutor:
    def __init__(self):
        self.comments = []
        self.tasks = []

    def comment(self, task_id: str, body: str):
        self.comments.append((task_id, body))
        return {"task_id": task_id, "commented": True}

    def create_task(self, *, title: str, assignee: str, body: str = "", parents=None, priority=None):
        task_id = "t_created123"
        self.tasks.append({"title": title, "assignee": assignee, "body": body, "parents": parents or [], "priority": priority})
        return {"task_id": task_id, "created": True}


def _deploy_html(tmp_path: Path, store: ArtifactStore, *, slug: str, password: str = "opensesame", capabilities=None):
    source = tmp_path / f"{slug}.html"
    source.write_text(f"<h1>{slug}</h1><script>localStorage.setItem('artifactd:test','1')</script>", encoding="utf-8")
    store.set_workspace_password(password)
    return store.deploy(source, slug=slug, auth_mode="profile", capabilities=capabilities or [])


def _login_and_manifest(client: TestClient, slug: str):
    login = client.post(f"/{slug}/login", data={"password": "opensesame"}, follow_redirects=False)
    assert login.status_code == 303
    manifest = client.get(f"/{slug}/_actions")
    assert manifest.status_code == 200
    return manifest.json()


def test_action_manifest_requires_protected_auth_and_returns_csrf(tmp_path: Path):
    store = ArtifactStore(tmp_path / "home")
    public_source = tmp_path / "public.html"
    public_source.write_text("<h1>public</h1>", encoding="utf-8")
    store.deploy(public_source, slug="public", capabilities=["artifact.describe"])
    _deploy_html(tmp_path, store, slug="protected", capabilities=["artifact.describe"])
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    public_response = client.get("/public/_actions")
    locked_response = client.get("/protected/_actions")
    manifest = _login_and_manifest(client, "protected")

    assert public_response.status_code == 403
    assert locked_response.status_code == 401
    assert manifest["slug"] == "protected"
    assert manifest["csrf_token"]
    assert [cap["name"] for cap in manifest["capabilities"]] == ["artifact.describe"]


def test_artifact_describe_action_requires_csrf_updates_metadata_and_audits(tmp_path: Path):
    store = ArtifactStore(tmp_path / "home")
    _deploy_html(tmp_path, store, slug="dashboard", capabilities=["artifact.describe"])
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))
    manifest = _login_and_manifest(client, "dashboard")

    missing_csrf = client.post("/dashboard/_actions/artifact.describe", json={"title": "New title"})
    updated = client.post(
        "/dashboard/_actions/artifact.describe",
        headers={"x-artifactd-csrf": manifest["csrf_token"]},
        json={"title": "New title", "description": "Better project surface"},
    )

    artifact = store.get("dashboard")
    audit = store.list_action_audit("dashboard")
    assert missing_csrf.status_code == 403
    assert updated.status_code == 200
    assert artifact.title == "New title"
    assert artifact.description == "Better project surface"
    assert audit[-1].capability == "artifact.describe"
    assert audit[-1].status == "ok"
    assert audit[-1].payload_hash
    assert "New title" not in audit[-1].payload_hash


def test_artifact_archive_action_hides_from_home_and_records_archive_page(tmp_path: Path):
    store = ArtifactStore(tmp_path / "home")
    _deploy_html(tmp_path, store, slug="old-plan", capabilities=["artifact.archive"])
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))
    manifest = _login_and_manifest(client, "old-plan")

    archived = client.post(
        "/old-plan/_actions/artifact.archive",
        headers={"x-artifactd-csrf": manifest["csrf_token"]},
        json={"reason": "Superseded by the new cockpit"},
    )
    home = client.get("/")
    archive = client.get("/archive")

    artifact = store.get("old-plan")
    assert archived.status_code == 200
    assert artifact.status == "archived"
    assert artifact.archive_reason == "Superseded by the new cockpit"
    assert "old-plan" not in home.text
    assert "old-plan" in archive.text


def test_kanban_comment_and_create_task_actions_use_schema_and_executor(tmp_path: Path):
    store = ArtifactStore(tmp_path / "home")
    _deploy_html(tmp_path, store, slug="work-cockpit", capabilities=["kanban.comment", "kanban.create_task"])
    executor = FakeKanbanExecutor()
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret", kanban_executor=executor))
    manifest = _login_and_manifest(client, "work-cockpit")
    headers = {"x-artifactd-csrf": manifest["csrf_token"]}

    invalid = client.post("/work-cockpit/_actions/kanban.comment", headers=headers, json={"task_id": "bad", "body": "nope"})
    comment = client.post("/work-cockpit/_actions/kanban.comment", headers=headers, json={"task_id": "t_ec62a109", "body": "Decision recorded from artifact."})
    task = client.post(
        "/work-cockpit/_actions/kanban.create_task",
        headers=headers,
        json={"title": "Review cockpit decision", "assignee": "palmer", "body": "Created from artifact.", "parents": ["t_ec62a109"]},
    )

    assert invalid.status_code == 422
    assert comment.status_code == 200
    assert task.status_code == 200
    assert executor.comments == [("t_ec62a109", "Decision recorded from artifact.")]
    assert executor.tasks[0]["title"] == "Review cockpit decision"
    assert executor.tasks[0]["parents"] == ["t_ec62a109"]


def test_approval_only_or_unallowed_capabilities_do_not_execute(tmp_path: Path):
    store = ArtifactStore(tmp_path / "home")
    _deploy_html(tmp_path, store, slug="review", capabilities=["draft.email"])
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))
    manifest = _login_and_manifest(client, "review")

    response = client.post(
        "/review/_actions/draft.email",
        headers={"x-artifactd-csrf": manifest["csrf_token"]},
        json={"to": "person@example.com", "body": "send this"},
    )

    assert response.status_code == 403
    assert "approval" in response.json()["detail"]
