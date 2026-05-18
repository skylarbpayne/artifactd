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


def test_deploy_directory_preserves_assets(tmp_path: Path):
    source = tmp_path / "site"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<img src='assets/logo.txt'>", encoding="utf-8")
    (source / "assets" / "logo.txt").write_text("LOGO", encoding="utf-8")
    store = ArtifactStore(tmp_path / "home")

    artifact = store.deploy(source, slug="site")

    assert artifact.slug == "site"
    assert (tmp_path / "home" / "sites" / "site" / "assets" / "logo.txt").read_text(encoding="utf-8") == "LOGO"
