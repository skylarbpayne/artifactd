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


def test_homepage_lists_artifacts_with_descriptions_and_search(tmp_path: Path):
    deck = tmp_path / "deck.html"
    deck.write_text("<h1>Deck</h1>", encoding="utf-8")
    website = tmp_path / "website.html"
    website.write_text("<h1>Website</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(deck, slug="spring-gala-deck", title="Spring Gala Deck", description="Sponsor presentation storyboard")
    store.deploy(website, slug="website-preview", title="Website Preview", description="Agora homepage copy review")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    home = client.get("/")
    filtered = client.get("/?q=sponsor")

    assert home.status_code == 200
    assert "Search artifacts" in home.text
    assert "Spring Gala Deck" in home.text
    assert "Sponsor presentation storyboard" in home.text
    assert "Website Preview" in home.text
    assert filtered.status_code == 200
    assert "Spring Gala Deck" in filtered.text
    assert "Website Preview" not in filtered.text


def test_homepage_hides_archived_artifacts_and_archive_page_lists_them(tmp_path: Path):
    active = tmp_path / "active.html"
    active.write_text("<h1>Active</h1>", encoding="utf-8")
    old = tmp_path / "old.html"
    old.write_text("<h1>Old</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(active, slug="active", title="Active Artifact")
    store.deploy(old, slug="old", title="Old Artifact")
    store.archive("old")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    home = client.get("/")
    archive = client.get("/archive")

    assert home.status_code == 200
    assert "Active Artifact" in home.text
    assert "Old Artifact" not in home.text
    assert archive.status_code == 200
    assert "Archived artifacts" in archive.text
    assert "Old Artifact" in archive.text
    assert "Active Artifact" not in archive.text


def test_interactive_gog_endpoint_requires_artifact_password(tmp_path: Path):
    source = tmp_path / "reauth.html"
    source.write_text("<h1>Reauth</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="gmail-reauth-cockpit", password="opensesame")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    locked = client.get("/gmail-reauth-cockpit/_interactive/gog/accounts")
    client.post("/gmail-reauth-cockpit/login", data={"password": "opensesame"}, follow_redirects=False)
    unlocked = client.get("/gmail-reauth-cockpit/_interactive/gog/accounts")

    assert locked.status_code == 401
    assert unlocked.status_code == 200
    assert "jacquelineaguilar030@gmail.com" in unlocked.text


def test_interactive_gog_endpoint_is_only_available_for_reauth_slug(tmp_path: Path):
    source = tmp_path / "other.html"
    source.write_text("<h1>Other</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="other")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    response = client.get("/other/_interactive/gog/accounts")

    assert response.status_code == 404
