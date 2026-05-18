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
