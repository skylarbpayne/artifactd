from pathlib import Path

from typer.testing import CliRunner

from artifactd.cli import app


def test_cli_deploy_list_and_protect_roundtrip(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "demo.html"
    source.write_text("<h1>Demo</h1>", encoding="utf-8")
    home = tmp_path / "home"

    deployed = runner.invoke(app, ["--home", str(home), "deploy", str(source), "--slug", "demo"])
    listed = runner.invoke(app, ["--home", str(home), "list"])
    protected = runner.invoke(app, ["--home", str(home), "protect", "demo", "--password", "secret"])
    listed_again = runner.invoke(app, ["--home", str(home), "list"])

    assert deployed.exit_code == 0
    assert "http://127.0.0.1:8787/demo" in deployed.output
    assert listed.exit_code == 0
    assert "demo" in listed.output
    assert "public" in listed.output
    assert protected.exit_code == 0
    assert listed_again.exit_code == 0
    assert "protected" in listed_again.output


def test_cli_deploy_and_list_print_public_url_when_base_is_configured(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "demo.html"
    source.write_text("<h1>Demo</h1>", encoding="utf-8")
    home = tmp_path / "home"

    deployed = runner.invoke(
        app,
        [
            "--home",
            str(home),
            "--public-base-url",
            "https://artifacts.skylarbpayne.com/",
            "deploy",
            str(source),
            "--slug",
            "demo",
        ],
    )
    listed = runner.invoke(
        app,
        ["--home", str(home), "--public-base-url", "https://artifacts.skylarbpayne.com/", "list"],
    )

    assert deployed.exit_code == 0
    assert "public_url=https://artifacts.skylarbpayne.com/demo" in deployed.output
    assert listed.exit_code == 0
    assert "https://artifacts.skylarbpayne.com/demo" in listed.output


def test_cli_deploy_accepts_description_and_list_shows_it(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "demo.html"
    source.write_text("<h1>Demo</h1>", encoding="utf-8")
    home = tmp_path / "home"

    deployed = runner.invoke(
        app,
        [
            "--home",
            str(home),
            "deploy",
            str(source),
            "--slug",
            "demo",
            "--title",
            "Demo Artifact",
            "--description",
            "Review board for homepage edits",
        ],
    )
    listed = runner.invoke(app, ["--home", str(home), "list"])

    assert deployed.exit_code == 0
    assert listed.exit_code == 0
    assert "Review board for homepage edits" in listed.output


def test_cli_describe_updates_existing_artifact_metadata(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "demo.html"
    source.write_text("<h1>Demo</h1>", encoding="utf-8")
    home = tmp_path / "home"
    runner.invoke(app, ["--home", str(home), "deploy", str(source), "--slug", "demo"])

    described = runner.invoke(
        app,
        [
            "--home",
            str(home),
            "describe",
            "demo",
            "--title",
            "Demo Preview",
            "--description",
            "Visual review board for Jacqueline",
        ],
    )
    listed = runner.invoke(app, ["--home", str(home), "list"])

    assert described.exit_code == 0
    assert "updated demo" in described.output
    assert "Demo Preview" in listed.output
    assert "Visual review board for Jacqueline" in listed.output


def test_cli_deploy_accepts_action_capabilities(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "demo.html"
    source.write_text("<h1>Demo</h1>", encoding="utf-8")
    home = tmp_path / "home"

    deployed = runner.invoke(
        app,
        [
            "--home",
            str(home),
            "deploy",
            str(source),
            "--slug",
            "demo",
            "--password",
            "secret",
            "--capability",
            "artifact.describe",
            "--capability",
            "kanban.comment",
        ],
    )
    listed = runner.invoke(app, ["--home", str(home), "list"])

    assert deployed.exit_code == 0
    assert "actions=artifact.describe,kanban.comment" in listed.output


def test_cli_archive_restore_list_filters_and_prune_dry_run(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "demo.html"
    source.write_text("<h1>Demo</h1>", encoding="utf-8")
    home = tmp_path / "home"
    runner.invoke(app, ["--home", str(home), "deploy", str(source), "--slug", "demo", "--expires-at", "10"])

    archived = runner.invoke(app, ["--home", str(home), "archive", "demo"])
    active_list = runner.invoke(app, ["--home", str(home), "list"])
    archive_list = runner.invoke(app, ["--home", str(home), "list", "--status", "archived"])
    restored = runner.invoke(app, ["--home", str(home), "restore", "demo"])
    pruned = runner.invoke(app, ["--home", str(home), "prune", "--dry-run", "--now", "20"])

    assert archived.exit_code == 0
    assert "archived demo" in archived.output
    assert active_list.exit_code == 0
    assert "no artifacts deployed" in active_list.output
    assert archive_list.exit_code == 0
    assert "demo" in archive_list.output
    assert "archived" in archive_list.output
    assert restored.exit_code == 0
    assert "restored demo" in restored.output
    assert pruned.exit_code == 0
    assert "would archive demo: expired" in pruned.output
