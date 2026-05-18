from pathlib import Path

from artifactd.security import hash_password, verify_password, sign_artifact_cookie, verify_artifact_cookie
from artifactd.store import ArtifactStore, sanitize_slug


def test_sanitize_slug_keeps_urls_boring_and_safe():
    assert sanitize_slug(" Investor Memo!! ") == "investor-memo"
    assert sanitize_slug("../secrets") == "secrets"


def test_password_hash_verifies_without_storing_plaintext():
    encoded = hash_password("correct horse battery staple")

    assert "correct horse" not in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_cookie_signature_is_slug_scoped():
    cookie = sign_artifact_cookie("demo", "test-secret")

    assert verify_artifact_cookie("demo", cookie, "test-secret")
    assert not verify_artifact_cookie("other", cookie, "test-secret")
    assert not verify_artifact_cookie("demo", cookie + "tamper", "test-secret")


def test_deploy_single_html_file_creates_artifact(tmp_path: Path):
    source = tmp_path / "demo.html"
    source.write_text("<h1>Hello</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")

    artifact = store.deploy(source, slug="Demo Artifact", password="secret")

    assert artifact.slug == "demo-artifact"
    assert artifact.has_password is True
    assert (tmp_path / "home" / "sites" / "demo-artifact" / "index.html").read_text(encoding="utf-8") == "<h1>Hello</h1>"
    assert store.get("demo-artifact").slug == "demo-artifact"


def test_deploy_stores_description_and_searches_metadata(tmp_path: Path):
    source = tmp_path / "deck.html"
    source.write_text("<h1>Deck</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")

    artifact = store.deploy(
        source,
        slug="spring-gala-deck",
        title="Spring Gala Deck",
        description="Visual storyboard for Jacqueline's sponsor presentation",
    )
    matches = list(store.search("sponsor"))

    assert artifact.description == "Visual storyboard for Jacqueline's sponsor presentation"
    assert store.get("spring-gala-deck").description == artifact.description
    assert [match.slug for match in matches] == ["spring-gala-deck"]


def test_deploy_directory_preserves_assets(tmp_path: Path):
    source = tmp_path / "site"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<img src='assets/logo.txt'>", encoding="utf-8")
    (source / "assets" / "logo.txt").write_text("LOGO", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")

    artifact = store.deploy(source, slug="site")

    assert artifact.slug == "site"
    assert (tmp_path / "home" / "sites" / "site" / "assets" / "logo.txt").read_text(encoding="utf-8") == "LOGO"


def test_update_metadata_changes_title_and_description_without_redeploying(tmp_path: Path):
    source = tmp_path / "site.html"
    source.write_text("<h1>Site</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="site")

    updated = store.update_metadata("site", title="Agora Site Preview", description="Before and after homepage edits")

    assert updated.title == "Agora Site Preview"
    assert updated.description == "Before and after homepage edits"
    assert [artifact.slug for artifact in store.search("homepage")] == ["site"]


def test_archive_restore_and_search_filters_active_by_default(tmp_path: Path):
    source = tmp_path / "site.html"
    source.write_text("<h1>Site</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="site", title="Site")

    archived = store.archive("site")

    assert archived.status == "archived"
    assert store.get("site").status == "archived"
    assert [artifact.slug for artifact in store.list()] == []
    assert [artifact.slug for artifact in store.list(status="archived")] == ["site"]
    assert [artifact.slug for artifact in store.search("site")] == []

    restored = store.restore("site")

    assert restored.status == "active"
    assert [artifact.slug for artifact in store.search("site")] == ["site"]


def test_redeploy_reactivates_archived_artifact(tmp_path: Path):
    source = tmp_path / "site.html"
    source.write_text("<h1>Site v1</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="site", title="Site")
    store.archive("site", reason="old")
    source.write_text("<h1>Site v2</h1>", encoding="utf-8")

    redeployed = store.deploy(source, slug="site", title="Site v2")

    assert redeployed.status == "active"
    assert redeployed.archived_at is None
    assert redeployed.archive_reason is None
    assert [artifact.slug for artifact in store.list()] == ["site"]


def test_pinned_artifacts_cannot_be_archived_or_pruned(tmp_path: Path):
    source = tmp_path / "keep.html"
    source.write_text("<h1>Keep</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(source, slug="keep", pinned=True, expires_at=10)

    archived = store.archive("keep")
    report = store.prune(now=20, dry_run=False)

    assert archived.status == "active"
    assert archived.pinned is True
    assert report == [{"slug": "keep", "action": "skip", "reason": "pinned"}]
    assert store.get("keep") is not None
    assert store.get("keep").status == "active"


def test_prune_archives_expired_active_artifacts_before_deleting(tmp_path: Path):
    public_source = tmp_path / "public.html"
    public_source.write_text("<h1>Public</h1>", encoding="utf-8")
    protected_source = tmp_path / "protected.html"
    protected_source.write_text("<h1>Protected</h1>", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")
    store.deploy(public_source, slug="public", expires_at=10)
    store.deploy(protected_source, slug="protected", password="secret", expires_at=10)
    dry_run = store.prune(now=20, dry_run=True)
    applied = store.prune(now=20, dry_run=False)

    assert dry_run == [
        {"slug": "protected", "action": "archive", "reason": "expired"},
        {"slug": "public", "action": "archive", "reason": "expired"},
    ]
    assert applied == dry_run
    assert store.get("public").status == "archived"
    assert store.get("protected").status == "archived"

    second_pass = store.prune(now=20, dry_run=False)

    assert second_pass == [
        {"slug": "protected", "action": "skip", "reason": "protected"},
        {"slug": "public", "action": "delete", "reason": "expired archived"},
    ]
    assert store.get("public") is None
    assert store.get("protected") is not None
