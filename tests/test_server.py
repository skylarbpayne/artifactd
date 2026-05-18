from pathlib import Path

from fastapi.testclient import TestClient

from artifactd.server import create_app
from artifactd.store import ArtifactStore


def test_unprotected_artifact_serves_index_and_asset(tmp_path: Path):
    source = tmp_path / "site"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<h1>Public</h1>", encoding="utf-8")
    (source / "assets" / "style.css").write_text("body{}", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="public")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    response = client.get("/public")
    asset = client.get("/public/assets/style.css")

    assert response.status_code == 200
    assert "<h1>Public</h1>" in response.text
    assert asset.status_code == 200
    assert asset.text == "body{}"


def test_protected_artifact_requires_password_then_sets_cookie(tmp_path: Path):
    source = tmp_path / "secret.html"
    source.write_text("<h1>Secret</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="secret", password="opensesame")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    locked = client.get("/secret")
    rejected = client.post("/secret/login", data={"password": "wrong"}, follow_redirects=False)
    accepted = client.post("/secret/login", data={"password": "opensesame"}, follow_redirects=False)
    unlocked = client.get("/secret")

    assert locked.status_code == 401
    assert "Password required" in locked.text
    assert rejected.status_code == 401
    assert accepted.status_code == 303
    assert unlocked.status_code == 200
    assert "<h1>Secret</h1>" in unlocked.text


def test_missing_artifact_404s(tmp_path: Path):
    ArtifactStore(tmp_path / "home")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    assert client.get("/nope").status_code == 404
