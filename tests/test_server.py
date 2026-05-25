import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from artifactd import interactive
from artifactd.interactive import _validate_redirect_url
from artifactd.server import create_app
from artifactd.store import ArtifactStore

def _deploy_profile_protected(store: ArtifactStore, source: Path, *, slug: str, password: str = "opensesame", capabilities=None):
    store.set_workspace_password(password)
    return store.deploy(source, slug=slug, auth_mode="profile", capabilities=capabilities or [])


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


def test_profile_protected_artifact_requires_workspace_password_then_sets_cookie(tmp_path: Path):
    source = tmp_path / "secret.html"
    source.write_text("<h1>Secret</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(store, source, slug="secret")
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
    store.deploy(deck, slug="spring-gala-deck", title="Spring Gala Deck", description="Sponsor presentation storyboard", capabilities=["artifact.describe"])
    store.deploy(website, slug="website-preview", title="Website Preview", description="Agora homepage copy review")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    home = client.get("/")
    filtered = client.get("/?q=sponsor")

    assert home.status_code == 200
    assert "Search things" in home.text
    assert "actions=" not in home.text
    assert "Actions: 1" in home.text
    assert "overflow-wrap: anywhere" in home.text
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


def test_artifact_state_requires_capability_and_persists_versioned_json(tmp_path: Path):
    source = tmp_path / "canvas.html"
    source.write_text("<h1>Canvas</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="plain")
    store.deploy(source, slug="canvas", capabilities=["artifact.state"])
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    forbidden = client.get("/plain/_state/board")
    missing = client.get("/canvas/_state/board")
    saved = client.put("/canvas/_state/board", json={"snapshot": {"records": {"shape:1": {"type": "geo"}}}, "client_id": "test"})
    loaded = client.get("/canvas/_state/board")
    conflict = client.put("/canvas/_state/board", json={"snapshot": {}, "expected_version": 0})

    assert forbidden.status_code == 403
    assert missing.status_code == 200
    assert missing.json()["exists"] is False
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert saved.json()["updated_by"] == "test"
    assert loaded.status_code == 200
    assert loaded.json()["exists"] is True
    assert loaded.json()["version"] == 1
    assert loaded.json()["snapshot"]["records"]["shape:1"]["type"] == "geo"
    assert conflict.status_code == 409


def test_protected_artifact_state_requires_workspace_login_or_share_token(tmp_path: Path):
    source = tmp_path / "canvas.html"
    source.write_text("<h1>Canvas</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(store, source, slug="protected-canvas", capabilities=["artifact.state"])
    token = store.create_share_override("protected-canvas")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    locked = client.get("/protected-canvas/_state/board")
    shared = client.put(f"/protected-canvas/_state/board?share={token}", json={"snapshot": {"ok": True}})
    client.post("/protected-canvas/login", data={"password": "opensesame"}, follow_redirects=False)
    loaded = client.get("/protected-canvas/_state/board")

    assert locked.status_code == 401
    assert shared.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["snapshot"] == {"ok": True}


def test_interactive_gog_endpoint_requires_workspace_password_and_scopes_palmer_accounts(tmp_path: Path):
    source = tmp_path / "reauth.html"
    source.write_text("<h1>Reauth</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(store, source, slug="gmail-reauth-cockpit")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    locked = client.get("/gmail-reauth-cockpit/_interactive/gog/accounts")
    client.post("/gmail-reauth-cockpit/login", data={"password": "opensesame"}, follow_redirects=False)
    unlocked = client.get("/gmail-reauth-cockpit/_interactive/gog/accounts")

    assert locked.status_code == 401
    assert unlocked.status_code == 200
    payload = unlocked.json()
    accounts = {item["account"]: item for item in payload["accounts"]}
    assert payload["profile"] == "palmer"
    assert "skylar.b.payne@gmail.com" in unlocked.text
    assert "me@skylarbpayne.com" in accounts
    assert accounts["jacquelineandskylar@gmail.com"]["profile"] == "palmer"
    assert "palmer@skylarbpayne.com" in accounts
    assert "jacquelineaguilar030@gmail.com" not in unlocked.text
    assert "jaguilar@y2lef.org" not in unlocked.text
    assert '"mutation_enabled":false' in unlocked.text


def test_interactive_gog_endpoint_scopes_echo_accounts_and_duplicate_wedding_profile(tmp_path: Path, monkeypatch):
    echo_home = tmp_path / "echo-artifacts"
    monkeypatch.setattr(interactive, "ECHO_ARTIFACT_HOME", echo_home)
    source = tmp_path / "reauth.html"
    source.write_text("<h1>Reauth</h1>", encoding="utf-8")
    store = ArtifactStore(echo_home)
    _deploy_profile_protected(store, source, slug="google-auth-repair-center")
    client = TestClient(create_app(echo_home, cookie_secret="test-secret"))

    locked = client.get("/google-auth-repair-center/_interactive/gog/accounts")
    client.post("/google-auth-repair-center/login", data={"password": "opensesame"}, follow_redirects=False)
    unlocked = client.get("/google-auth-repair-center/_interactive/gog/accounts")
    wedding_status = client.get(
        "/google-auth-repair-center/_interactive/gog/status",
        params={"account": "jacquelineandskylar@gmail.com"},
    )

    assert locked.status_code == 401
    assert unlocked.status_code == 200
    payload = unlocked.json()
    accounts = {item["account"]: item for item in payload["accounts"]}
    assert payload["profile"] == "echo"
    assert set(accounts) == {
        "jacquelineaguilar030@gmail.com",
        "jaguilar@y2lef.org",
        "jacquelineandskylar@gmail.com",
    }
    assert accounts["jacquelineandskylar@gmail.com"]["profile"] == "echo"
    assert "skylar.b.payne@gmail.com" not in unlocked.text
    assert "me@skylarbpayne.com" not in unlocked.text
    assert "palmer@skylarbpayne.com" not in unlocked.text
    assert wedding_status.status_code == 200
    assert wedding_status.json()["profile"] == "echo"
    assert wedding_status.json()["status"] == "idle"


def test_interactive_gog_start_is_approval_gated_without_capability(tmp_path: Path):
    source = tmp_path / "repair.html"
    source.write_text("<h1>Repair</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(store, source, slug="google-auth-repair-center")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))
    client.post("/google-auth-repair-center/login", data={"password": "opensesame"}, follow_redirects=False)

    response = client.post(
        "/google-auth-repair-center/_interactive/gog/start",
        data={"account": "skylar.b.payne@gmail.com"},
    )

    assert response.status_code == 403
    assert "not enabled" in response.text
    audits = store.list_action_audit("google-auth-repair-center")
    assert len(audits) == 1
    assert audits[0].capability == "gog.reauth"
    assert audits[0].status == "denied"
    assert "missing capability" in audits[0].error


def test_interactive_gog_start_is_approval_gated_without_server_approval(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ARTIFACTD_ENABLE_GOG_REAUTH_ACTIONS", raising=False)
    monkeypatch.setenv("ARTIFACTD_GOG_REAUTH_APPROVAL_FILE", str(tmp_path / "missing-approval-marker"))
    source = tmp_path / "repair.html"
    source.write_text("<h1>Repair</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(
        store,
        source,
        slug="google-auth-repair-center",
        capabilities=["gog.reauth"],
    )
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))
    client.post("/google-auth-repair-center/login", data={"password": "opensesame"}, follow_redirects=False)

    response = client.post(
        "/google-auth-repair-center/_interactive/gog/start",
        data={"account": "skylar.b.payne@gmail.com"},
    )

    assert response.status_code == 403
    assert "approval gate" in response.text
    audits = store.list_action_audit("google-auth-repair-center")
    assert len(audits) == 1
    assert audits[0].status == "denied"
    assert "server approval disabled" in audits[0].error


def test_interactive_gog_endpoint_is_only_available_for_reauth_slug(tmp_path: Path):
    source = tmp_path / "other.html"
    source.write_text("<h1>Other</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="other")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    response = client.get("/other/_interactive/gog/accounts")

    assert response.status_code == 404


def test_audio_intake_upload_requires_workspace_password_and_capability(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(interactive, "AUDIO_INTAKE_DIR", tmp_path / "intake")
    source = tmp_path / "intake.html"
    source.write_text("<h1>Meeting Notes Intake</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(store, source, slug="meeting-notes-intake")
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    locked = client.post(
        "/meeting-notes-intake/_interactive/audio/upload",
        files={"audio": ("voice.m4a", b"fake audio", "audio/mp4")},
    )
    client.post("/meeting-notes-intake/login", data={"password": "opensesame"}, follow_redirects=False)
    missing_capability = client.post(
        "/meeting-notes-intake/_interactive/audio/upload",
        files={"audio": ("voice.m4a", b"fake audio", "audio/mp4")},
    )

    assert locked.status_code == 401
    assert missing_capability.status_code == 403
    assert "Audio intake is staged" in missing_capability.text


def test_audio_intake_upload_saves_metadata_and_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(interactive, "AUDIO_INTAKE_DIR", tmp_path / "intake")
    source = tmp_path / "intake.html"
    source.write_text("<h1>Meeting Notes Intake</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(
        store,
        source,
        slug="meeting-notes-intake",
        capabilities=["audio.intake"],
    )
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))
    client.post("/meeting-notes-intake/login", data={"password": "opensesame"}, follow_redirects=False)
    monkeypatch.setattr(interactive, "_start_audio_processing", lambda upload_id: interactive._write_audio_intake_metadata(str(upload_id), {"status": "queued", "progress": "Queued for test."}))

    response = client.post(
        "/meeting-notes-intake/_interactive/audio/upload",
        data={"meeting_title": "Board prep", "context": "Use concise actions."},
        files={"audio": ("voice note.m4a", b"fake audio", "audio/mp4")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["progress"] == "Queued for test."
    assert payload["meeting_title"] == "Board prep"
    assert payload["file_name"] == "voice-note.m4a"
    assert Path(payload["audio_path"]).read_bytes() == b"fake audio"
    assert payload["echo_prompt"].endswith("and produce meeting notes.")
    status = client.get(
        "/meeting-notes-intake/_interactive/audio/status",
        params={"upload_id": payload["upload_id"]},
    )
    assert status.status_code == 200
    assert status.json()["sha256"] == payload["sha256"]
    audits = store.list_action_audit("meeting-notes-intake")
    assert len(audits) == 1
    assert audits[0].capability == "audio.intake"


def test_audio_intake_processing_writes_notes_to_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(interactive, "AUDIO_INTAKE_DIR", tmp_path / "intake")
    upload_id = "20260519-160000-abcdef1234"
    job_dir = tmp_path / "intake" / upload_id
    job_dir.mkdir(parents=True)
    audio_path = job_dir / "voice.m4a"
    audio_path.write_bytes(b"fake audio")
    (job_dir / "metadata.json").write_text(
        '{"upload_id":"20260519-160000-abcdef1234","status":"uploaded","audio_path":"'
        + str(audio_path)
        + '","job_dir":"'
        + str(job_dir)
        + '","meeting_title":"Board prep","context":"","output_style":"standard"}',
        encoding="utf-8",
    )

    def fake_run(cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("--out") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "transcript.txt").write_text("Jacqueline will send the agenda by Friday.", encoding="utf-8")
        (out_dir / "metadata.json").write_text('{"provider":"local faster-whisper","model":"base","segments":1}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    monkeypatch.setattr(interactive.subprocess, "run", fake_run)
    monkeypatch.setattr(interactive, "_generate_meeting_notes", lambda metadata, transcript: "**Meeting notes — Board prep**\n\n- [ ] Send agenda — Owner: Jacqueline; Due: Friday; Context: explicit transcript commitment.")

    interactive._process_audio_upload(upload_id)

    metadata = interactive._read_audio_intake_metadata(upload_id)
    assert metadata["status"] == "ready"
    assert "Send agenda" in metadata["meeting_notes"]
    assert Path(metadata["meeting_notes_path"]).exists()


def test_audio_library_lists_searches_updates_and_downloads_notes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(interactive, "AUDIO_INTAKE_DIR", tmp_path / "intake")
    source = tmp_path / "intake.html"
    source.write_text("<h1>Meeting Notes Intake</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    _deploy_profile_protected(
        store,
        source,
        slug="meeting-notes-intake",
        capabilities=["audio.intake"],
    )
    upload_id = "20260519-170000-abcdef1234"
    job_dir = tmp_path / "intake" / upload_id
    transcript_dir = job_dir / "transcription"
    transcript_dir.mkdir(parents=True)
    notes = "**Meeting notes — Board prep**\n\n- [ ] Send agenda — Owner: Jacqueline; Due: Friday; Context: transcript commitment."
    (transcript_dir / "transcript.txt").write_text("Maria mentioned donor outreach.", encoding="utf-8")
    (job_dir / "metadata.json").write_text(
        json.dumps(
            {
                "upload_id": upload_id,
                "status": "ready",
                "created_at": 1779210000,
                "completed_at": 1779210300,
                "file_name": "board.m4a",
                "meeting_title": "Board prep",
                "transcript_path": str(transcript_dir / "transcript.txt"),
                "job_dir": str(job_dir),
                "meeting_notes": notes,
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(tmp_path / "home", cookie_secret="test-secret"))

    locked = client.get("/meeting-notes-intake/_interactive/audio/library")
    client.post("/meeting-notes-intake/login", data={"password": "opensesame"}, follow_redirects=False)
    listing = client.get("/meeting-notes-intake/_interactive/audio/library")
    transcript_search = client.get("/meeting-notes-intake/_interactive/audio/library", params={"q": "donor"})
    update = client.post(
        "/meeting-notes-intake/_interactive/audio/update",
        data={
            "upload_id": upload_id,
            "meeting_title": "Renamed board prep",
            "meeting_date": "2026-05-19",
            "tags": "Y2L, Follow-up needed, y2l",
            "notes": "**Edited notes**\n\n- [ ] Confirm room — Owner: Skylar; Due: today; Context: logistics.",
            "follow_up_needed": "true",
        },
    )
    filtered = client.get("/meeting-notes-intake/_interactive/audio/library", params={"tag": "Y2L", "q": "confirm room"})
    download = client.get("/meeting-notes-intake/_interactive/audio/download", params={"upload_id": upload_id})

    assert locked.status_code == 401
    assert listing.status_code == 200
    assert listing.json()["meetings"][0]["meeting_title"] == "Board prep"
    assert transcript_search.json()["total_matches"] == 1
    assert update.status_code == 200
    payload = update.json()
    assert payload["meeting_title"] == "Renamed board prep"
    assert payload["meeting_date"] == "2026-05-19"
    assert payload["tags"] == ["Y2L", "Follow-up needed"]
    assert payload["follow_up_needed"] is True
    assert payload["has_edits"] is True
    assert "Confirm room" in payload["effective_notes"]
    assert payload["transcript"] == "Maria mentioned donor outreach."
    assert Path(payload["edited_meeting_notes_path"]).exists()
    assert filtered.json()["total_matches"] == 1
    assert download.status_code == 200
    assert "Confirm room" in download.text
    assert "renamed-board-prep.md" in download.headers["content-disposition"]


@pytest.mark.parametrize(
    "redirect_url",
    [
        "http://localhost:1?code=fake-code&state=fake-state",
        "http://localhost:1/?code=fake-code&state=fake-state",
        "http://127.0.0.1:1?code=fake-code&state=fake-state",
        "http://127.0.0.1:1/?code=fake-code&state=fake-state",
    ],
)
def test_google_redirect_validation_accepts_localhost_port_one_with_or_without_slash(redirect_url: str):
    _validate_redirect_url(redirect_url)


@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://localhost:1?code=fake-code",
        "http://localhost:2?code=fake-code",
        "http://example.com:1?code=fake-code",
        "http://localhost:1?state=fake-state",
        "not-a-url",
    ],
)
def test_google_redirect_validation_rejects_non_matching_redirects(redirect_url: str):
    with pytest.raises(Exception):
        _validate_redirect_url(redirect_url)
