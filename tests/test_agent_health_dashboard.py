import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_health_dashboard import process_inventory, codex_auth_summary, render_html, Check


def test_process_inventory_groups_agent_runtime_processes_without_leaking_full_commands():
    ps_output = """  101 artifactd /Users/skylarpayne/artifactd/.venv/bin/artifactd serve --port 8787 --secret-token abc123
  102 cloudflared /opt/homebrew/bin/cloudflared tunnel run artifacts
  103 python /Users/skylarpayne/.hermes/hermes-agent/venv/bin/hermes gateway start -p palmer
  104 codex /opt/homebrew/bin/codex exec build this
  105 bash /bin/bash -lc sleep 10
"""

    inventory = process_inventory(ps_output)

    assert inventory["counts"] == {
        "artifactd": 1,
        "cloudflared": 1,
        "hermes": 1,
        "codex": 1,
        "other": 1,
    }
    assert inventory["total_tracked"] == 4
    assert inventory["samples"]["artifactd"] == ["101 artifactd serve --port 8787"]
    assert "secret-token" not in str(inventory)


def test_codex_auth_summary_reports_presence_without_token_contents(tmp_path):
    auth_file = tmp_path / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text('{"OPENAI_API_KEY":"sk-secret", "tokens":"do-not-show"}', encoding="utf-8")

    summary = codex_auth_summary(tmp_path)

    assert summary["status"] == "ok"
    assert summary["evidence"] == "Codex auth file exists and is non-empty"
    assert "secret" not in str(summary)
    assert "do-not-show" not in str(summary)


def test_render_html_includes_ops_console_language():
    html = render_html(
        [
            Check(
                id="runtime-processes",
                agent="Palmer host",
                app="Runtime processes",
                account="local machine",
                operation="ps inventory",
                status="ok",
                summary="Agent runtime processes visible",
                evidence="artifactd=1, hermes=2",
            )
        ],
        "2026-05-21 19:30 PDT",
    )

    assert "ops console" in html.lower()
    assert "Runtime processes" in html
    assert "Codex" in html
