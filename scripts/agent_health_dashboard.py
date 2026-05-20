#!/usr/bin/env python3
"""Generate a protected Palmer/Echo connectivity health artifact.

The artifact is intentionally static: it captures the last live smoke-test run,
shows exactly which app/account broke, and gives targeted repair commands so a
single bad email token does not turn into a full re-auth clown parade.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

PUBLIC_BASE_URL = "https://artifacts.skylarbpayne.com"
ARTIFACT_SLUG = "agent-health"
PALMER_PROFILE = pathlib.Path("/Users/skylarpayne/.hermes/profiles/palmer")
ECHO_PROFILE = pathlib.Path("/Users/skylarpayne/.hermes/profiles/echo")
ARTIFACT_HOME = pathlib.Path("/Users/skylarpayne/.hermes/artifacts")
SKILL_CANVA = PALMER_PROFILE / "skills/productivity/third-party-oauth-integrations/scripts/canva_oauth.py"

STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2, "unknown": 3}


@dataclass
class Check:
    id: str
    agent: str
    app: str
    account: str
    operation: str
    status: str
    summary: str
    evidence: str = ""
    remediation: str = ""
    seconds: float = 0.0


def load_profile_env(profile: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(profile),
            "HOME": str(profile / "home"),
            "GOG_CONFIG_DIR": str(profile / "home/Library/Application Support/gogcli"),
        }
    )
    env_path = profile / ".env"
    if env_path.exists():
        for raw in env_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key] = value.strip().strip('"').strip("'")
    return env


def run(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 25) -> tuple[int, str, str, float]:
    start = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - start
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timeout", time.time() - start
    except FileNotFoundError as exc:
        return 127, "", str(exc), time.time() - start


def redact(text: str, limit: int = 260) -> str:
    text = (text or "").replace("\x00", "")
    text = re.sub(r"gh[oprsu]_[A-Za-z0-9_]+", "gh*_REDACTED", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer REDACTED", text)
    text = re.sub(r"(?i)(access_token|refresh_token|client_secret|password)\"?\s*[:=]\s*\"?[^\"\s,}]+", r"\1=REDACTED", text)
    text = re.sub(r"code=[^&\s]+", "code=REDACTED", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def parse_json(text: str) -> Any | None:
    try:
        return json.loads(text, strict=False)
    except Exception:
        return None


def add_check(checks: list[Check], **kwargs: Any) -> None:
    checks.append(Check(**kwargs))


def gog_smoke(checks: list[Check], agent: str, env: dict[str, str], account: str) -> None:
    # Gmail: broad recent search; empty results are okay if the API succeeds.
    rc, out, err, secs = run(
        ["gog", "gmail", "search", "newer_than:7d", "--account", account, "--json", "--max=1", "--no-input"],
        env,
        30,
    )
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict):
        threads = data.get("threads") or []
        subject = threads[0].get("subject", "API returned no recent threads") if threads else "API returned no recent threads"
        status = "ok"
        summary = "Gmail read smoke passed"
        evidence = f"latest subject: {subject}"
        remediation = ""
    else:
        status = "fail"
        summary = "Gmail read smoke failed"
        evidence = redact(err or out, 360)
        remediation = targeted_gog_reauth(agent, account)
    add_check(
        checks,
        id=f"{agent.lower()}-gmail-{account}",
        agent=agent,
        app="Gmail",
        account=account,
        operation="search newer_than:7d",
        status=status,
        summary=summary,
        evidence=evidence,
        remediation=remediation,
        seconds=secs,
    )

    # Calendar: query primary next 7 days; empty events are still healthy.
    rc, out, err, secs = run(
        ["gog", "calendar", "events", "primary", "--account", account, "--days=7", "--json", "--max=1", "--no-input"],
        env,
        30,
    )
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and "events" in data:
        events = data.get("events") or []
        event = events[0].get("summary", "calendar reachable; no upcoming events returned") if events else "calendar reachable; no upcoming events returned"
        status = "ok"
        summary = "Calendar read smoke passed"
        evidence = f"sample: {event}"
        remediation = ""
    else:
        status = "fail"
        summary = "Calendar read smoke failed"
        evidence = redact(err or out, 360)
        remediation = targeted_gog_reauth(agent, account)
    add_check(
        checks,
        id=f"{agent.lower()}-calendar-{account}",
        agent=agent,
        app="Google Calendar",
        account=account,
        operation="events primary next 7d",
        status=status,
        summary=summary,
        evidence=evidence,
        remediation=remediation,
        seconds=secs,
    )


def gog_drive_smoke(checks: list[Check], agent: str, env: dict[str, str], account: str) -> None:
    rc, out, err, secs = run(
        ["gog", "drive", "ls", "--account", account, "--json", "--max", "1", "--no-input"],
        env,
        30,
    )
    data = parse_json(out)
    if rc == 0 and data is not None:
        status = "ok"
        summary = "Drive read smoke passed"
        evidence = "Drive API reachable"
        remediation = ""
    else:
        status = "fail"
        summary = "Drive read smoke failed"
        evidence = redact(err or out, 360)
        remediation = targeted_gog_reauth(agent, account)
    add_check(
        checks,
        id=f"{agent.lower()}-drive-{account}",
        agent=agent,
        app="Google Drive",
        account=account,
        operation="drive ls max 1",
        status=status,
        summary=summary,
        evidence=evidence,
        remediation=remediation,
        seconds=secs,
    )


def targeted_gog_reauth(agent: str, account: str) -> str:
    profile = "palmer" if agent == "Palmer" else "echo"
    base = f"/Users/skylarpayne/.hermes/profiles/{profile}"
    return (
        "Targeted repair only — do not wipe every account. Run in a local shell: "
        f"export HERMES_HOME={base}; export HOME={base}/home; "
        "export GOG_CONFIG_DIR=\"$HOME/Library/Application Support/gogcli\"; "
        f"gog auth remove {account} --force --no-input || true; "
        f"gog auth add {account} --manual --services gmail,calendar,drive,docs,sheets --drive-scope readonly --force-consent; "
        f"gog gmail search 'newer_than:7d' --account {account} --json --max=1 --no-input"
    )


def check_outlook(checks: list[Check]) -> None:
    env = os.environ.copy()
    env["MSGCLI_CONFIG_DIR"] = "/Users/skylarpayne/.msgcli"
    rc, out, err, secs = run(["msgcli", "auth", "status", "--no-input", "-o", "json"], env, 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and data.get("accounts"):
        valid = [a for a in data.get("accounts", []) if a.get("valid")]
        status = "ok" if valid else "fail"
        evidence = ", ".join(a.get("email", "unknown") for a in valid) or "no valid account"
        remediation = "" if valid else "Run msgcli device-code auth for jacqueline@jacquelinepayne.com with MSGCLI_CONFIG_DIR=/Users/skylarpayne/.msgcli, then rerun this dashboard."
        summary = "Outlook Graph auth valid" if valid else "Outlook Graph auth has no valid accounts"
    else:
        status = "fail"
        summary = "Outlook auth status failed"
        evidence = redact(err or out, 360)
        remediation = "Check MSGCLI_CONFIG_DIR=/Users/skylarpayne/.msgcli, then run msgcli auth/device-code repair."
    add_check(checks, id="echo-outlook-auth", agent="Echo", app="Outlook", account="jacqueline@jacquelinepayne.com", operation="msgcli auth status", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)

    rc, out, err, secs = run(["msgcli", "mail", "list", "-a", "jacqueline@jacquelinepayne.com", "--limit", "1", "--no-input", "-o", "json"], env, 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, list):
        status = "ok"
        summary = "Outlook mail read smoke passed"
        subject = data[0].get("subject", "mailbox reachable; no message returned") if data else "mailbox reachable; no message returned"
        evidence = f"latest subject: {subject}"
        remediation = ""
    else:
        status = "fail"
        summary = "Outlook mail read smoke failed"
        evidence = redact(err or out, 360)
        remediation = "Repair msgcli auth before trying Himalaya/app-password paths."
    add_check(checks, id="echo-outlook-mail", agent="Echo", app="Outlook", account="jacqueline@jacquelinepayne.com", operation="mail list limit 1", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_asana(checks: list[Check], env: dict[str, str]) -> None:
    token = env.get("ASANA_API_KEY")
    if not token:
        add_check(checks, id="echo-asana", agent="Echo", app="Asana", account="Payne HQ", operation="/users/me", status="fail", summary="ASANA_API_KEY missing", evidence="Echo profile .env has no ASANA_API_KEY", remediation="Add Echo's Asana PAT to /Users/skylarpayne/.hermes/profiles/echo/.env, then smoke-test /users/me.")
        return
    rc, out, err, secs = run(["curl", "-fsS", "-H", f"Authorization: Bearer {token}", "https://app.asana.com/api/1.0/users/me"], env, 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and data.get("data"):
        me = data["data"]
        workspaces = ", ".join(w.get("name", "unknown") for w in me.get("workspaces", []))
        status = "ok"
        summary = "Asana API token works"
        evidence = f"identity: {me.get('email', 'unknown')}; workspaces: {workspaces or 'none returned'}"
        remediation = ""
    else:
        status = "fail"
        summary = "Asana API token smoke failed"
        evidence = redact(err or out, 360)
        remediation = "Replace ASANA_API_KEY in Echo profile .env and verify /users/me. Do not print the token."
    add_check(checks, id="echo-asana", agent="Echo", app="Asana", account="Payne HQ", operation="GET /users/me", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_canva(checks: list[Check], env: dict[str, str]) -> None:
    if not SKILL_CANVA.exists():
        add_check(checks, id="echo-canva", agent="Echo", app="Canva", account="Echo profile", operation="Canva smoke helper", status="unknown", summary="Canva helper script missing", evidence=str(SKILL_CANVA), remediation="Restore third-party-oauth-integrations skill or verify Canva manually.")
        return
    rc, out, err, secs = run(["python3", str(SKILL_CANVA), "smoke"], env, 30)
    if rc == 0:
        status = "ok"
        summary = "Canva Connect smoke passed"
        evidence = redact(out, 320)
        remediation = ""
    else:
        missing_secret = "Missing CANVA_CLIENT_ID" in (err + out)
        status = "warn" if missing_secret else "fail"
        summary = "Canva not wired in Echo profile" if missing_secret else "Canva smoke failed"
        evidence = redact(err or out, 360)
        remediation = "If Echo needs Canva API access, add CANVA_CLIENT_ID/CANVA_CLIENT_SECRET to Echo .env and run the Canva PKCE flow; until then use browser/manual Canva, not agent API calls."
    add_check(checks, id="echo-canva", agent="Echo", app="Canva", account="Echo profile", operation="canva_oauth.py smoke", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_github(checks: list[Check]) -> None:
    env = os.environ.copy()
    env["HOME"] = "/Users/skylarpayne"
    rc, out, err, secs = run(["gh", "auth", "status"], env, 20)
    if rc == 0:
        status = "ok"
        summary = "GitHub CLI auth works"
        evidence = redact(out, 300)
        remediation = ""
    else:
        status = "fail"
        summary = "GitHub CLI auth failed"
        evidence = redact(err or out, 360)
        remediation = "Use HOME=/Users/skylarpayne or GH_CONFIG_DIR=/Users/skylarpayne/.config/gh; if still failing, run gh auth login in Skylar's real home."
    add_check(checks, id="github-gh", agent="Palmer + Echo", app="GitHub", account="skylarbpayne", operation="gh auth status", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_kanban_artifactd(checks: list[Check]) -> None:
    rc, out, err, secs = run(["hermes", "-p", "palmer", "kanban", "stats", "--json"], os.environ.copy(), 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and data.get("by_status"):
        status = "ok"
        summary = "Hermes Kanban reachable"
        evidence = "statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(data["by_status"].items()))
        remediation = ""
    else:
        status = "fail"
        summary = "Hermes Kanban stats failed"
        evidence = redact(err or out, 360)
        remediation = "Check shared DB /Users/skylarpayne/.hermes/kanban.db and hermes CLI path before blaming workers."
    add_check(checks, id="palmer-kanban", agent="Palmer", app="Hermes Kanban", account="shared root board", operation="kanban stats", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)

    rc, out, err, secs = run(["curl", "-fsS", "https://artifacts.skylarbpayne.com/smoke-live"], os.environ.copy(), 20)
    if rc == 0:
        status = "ok"
        summary = "artifactd public tunnel reachable"
        evidence = "GET /smoke-live returned content"
        remediation = ""
    else:
        status = "fail"
        summary = "artifactd public smoke failed"
        evidence = redact(err or out, 360)
        remediation = "Check system LaunchDaemons for artifactd/cloudflared, then verify local 127.0.0.1:8787 before DNS/tunnel debugging."
    add_check(checks, id="artifactd-public", agent="Palmer", app="artifactd", account="artifacts.skylarbpayne.com", operation="HTTPS smoke-live", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def collect_checks() -> list[Check]:
    checks: list[Check] = []
    palmer_env = load_profile_env(PALMER_PROFILE)
    echo_env = load_profile_env(ECHO_PROFILE)

    palmer_google = [
        "skylar.b.payne@gmail.com",
        "me@skylarbpayne.com",
        "jacquelineandskylar@gmail.com",
        "palmer@skylarbpayne.com",
    ]
    echo_google = [
        "jacquelineaguilar030@gmail.com",
        "jaguilar@y2lef.org",
    ]
    for account in palmer_google:
        gog_smoke(checks, "Palmer", palmer_env, account)
    # Drive checks are slower/noisier; smoke the two accounts most likely used for file work.
    for account in ["me@skylarbpayne.com", "palmer@skylarbpayne.com"]:
        gog_drive_smoke(checks, "Palmer", palmer_env, account)
    for account in echo_google:
        gog_smoke(checks, "Echo", echo_env, account)
    gog_drive_smoke(checks, "Echo", echo_env, "jacquelineaguilar030@gmail.com")

    check_outlook(checks)
    check_asana(checks, echo_env)
    check_canva(checks, echo_env)
    check_github(checks)
    check_kanban_artifactd(checks)
    return checks


def rollup(checks: list[Check]) -> dict[str, Any]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    if counts["fail"]:
        overall = "fail"
        label = "Broken checks need attention"
    elif counts["warn"]:
        overall = "warn"
        label = "Mostly healthy; one non-critical gap"
    else:
        overall = "ok"
        label = "Healthy"
    return {"overall": overall, "label": label, "counts": counts}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_html(checks: list[Check], generated_at: str) -> str:
    summary = rollup(checks)
    by_agent: dict[str, list[Check]] = {}
    for check in sorted(checks, key=lambda c: (c.agent, STATUS_ORDER.get(c.status, 9), c.app, c.account)):
        by_agent.setdefault(check.agent, []).append(check)

    def badge(status: str) -> str:
        return f'<span class="badge {esc(status)}">{esc(status.upper())}</span>'

    def status_dot(status: str) -> str:
        return f'<span class="dot {esc(status)}"></span>'

    def check_card(c: Check) -> str:
        remediation = f'<details><summary>Repair path</summary><pre>{esc(c.remediation)}</pre></details>' if c.remediation else ""
        return f"""
        <article class="check {esc(c.status)}" id="{esc(c.id)}">
          <div class="check-head">
            <div>{status_dot(c.status)}<strong>{esc(c.app)}</strong> <span class="muted">· {esc(c.account)}</span></div>
            {badge(c.status)}
          </div>
          <div class="summary">{esc(c.summary)}</div>
          <div class="meta"><span>{esc(c.operation)}</span><span>{c.seconds:.2f}s</span></div>
          <p class="evidence">{esc(c.evidence)}</p>
          {remediation}
        </article>
        """

    grouped = "\n".join(
        f"""
        <section class="agent">
          <h2>{esc(agent)}</h2>
          <div class="checks">{''.join(check_card(c) for c in items)}</div>
        </section>
        """
        for agent, items in by_agent.items()
    )

    failing = [c for c in checks if c.status in {"fail", "warn"}]
    incident_rows = "".join(
        f"<li>{badge(c.status)} <strong>{esc(c.agent)} / {esc(c.app)}</strong> — {esc(c.account)}: {esc(c.summary)}</li>"
        for c in failing
    ) or "<li>Nothing currently broken. Weirdly peaceful.</li>"

    json_blob = esc(json.dumps({"generated_at": generated_at, "summary": summary, "checks": [asdict(c) for c in checks]}, indent=2))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Palmer / Echo Health</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #080b12; --panel: #111827; --panel2: #0f172a; --text: #e5e7eb; --muted: #94a3b8;
      --ok: #22c55e; --warn: #f59e0b; --fail: #ef4444; --unknown: #94a3b8; --line: rgba(148,163,184,.22);
      --accent: #38bdf8;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, rgba(56,189,248,.16), transparent 34rem), var(--bg); color: var(--text); }}
    header {{ padding: 36px 28px 20px; max-width: 1180px; margin: 0 auto; }}
    h1 {{ margin: 0; font-size: clamp(32px, 5vw, 58px); letter-spacing: -.05em; line-height: .95; }}
    h2 {{ margin: 0 0 14px; font-size: 22px; letter-spacing: -.02em; }}
    h3 {{ margin: 0 0 10px; }}
    p {{ color: var(--muted); }}
    code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    .topline {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 18px; }}
    .hero {{ display: grid; grid-template-columns: 1.2fr .8fr; gap: 18px; margin-top: 24px; }}
    .panel {{ background: linear-gradient(180deg, rgba(17,24,39,.96), rgba(15,23,42,.94)); border: 1px solid var(--line); border-radius: 22px; padding: 20px; box-shadow: 0 24px 80px rgba(0,0,0,.35); }}
    .status-big {{ display: flex; align-items: center; gap: 14px; font-size: 25px; font-weight: 750; }}
    .counts {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px; }}
    .count {{ background: rgba(15,23,42,.85); border: 1px solid var(--line); border-radius: 16px; padding: 14px; }}
    .count b {{ display: block; font-size: 28px; }}
    .count span {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 0 28px 48px; }}
    .agent {{ margin-top: 22px; }}
    .checks {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .check {{ background: rgba(15,23,42,.88); border: 1px solid var(--line); border-left: 5px solid var(--unknown); border-radius: 18px; padding: 16px; min-height: 172px; }}
    .check.ok {{ border-left-color: var(--ok); }} .check.warn {{ border-left-color: var(--warn); }} .check.fail {{ border-left-color: var(--fail); }}
    .check-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .summary {{ margin-top: 10px; font-weight: 650; }}
    .meta {{ display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 12px; margin-top: 8px; }}
    .evidence {{ margin: 12px 0 0; font-size: 13px; line-height: 1.45; }}
    .muted {{ color: var(--muted); }}
    .badge {{ border: 1px solid currentColor; border-radius: 999px; padding: 4px 9px; font-size: 11px; font-weight: 800; letter-spacing: .08em; }}
    .badge.ok {{ color: var(--ok); }} .badge.warn {{ color: var(--warn); }} .badge.fail {{ color: var(--fail); }} .badge.unknown {{ color: var(--unknown); }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--unknown); margin-right: 9px; box-shadow: 0 0 18px currentColor; }}
    .dot.ok {{ background: var(--ok); color: var(--ok); }} .dot.warn {{ background: var(--warn); color: var(--warn); }} .dot.fail {{ background: var(--fail); color: var(--fail); }}
    details {{ margin-top: 12px; }}
    summary {{ cursor: pointer; color: #bae6fd; font-weight: 700; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: rgba(2,6,23,.85); border: 1px solid var(--line); border-radius: 14px; padding: 12px; color: #d1fae5; font-size: 12px; line-height: 1.45; }}
    .playbook {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 22px; }}
    .playbook li {{ margin: 8px 0; color: var(--muted); }}
    .incident li {{ margin: 9px 0; color: var(--muted); }}
    .footer {{ margin-top: 22px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 850px) {{ .hero, .checks, .playbook {{ grid-template-columns: 1fr; }} .counts {{ grid-template-columns: repeat(2,1fr); }} }}
  </style>
</head>
<body>
  <header>
    <div class="topline">{badge(summary['overall'])}<span class="muted">Generated {esc(generated_at)}</span><span class="muted">Protected artifact: /{ARTIFACT_SLUG}</span></div>
    <h1>Palmer / Echo<br/>connectivity health</h1>
    <div class="hero">
      <section class="panel">
        <div class="status-big">{status_dot(summary['overall'])}{esc(summary['label'])}</div>
        <p>This is the boring dashboard we actually need: live smoke tests by profile, app, and account. The goal is to avoid the dumb failure mode where one Gmail token expires and we nuke every integration from orbit.</p>
        <div class="counts">
          <div class="count"><b>{summary['counts'].get('ok', 0)}</b><span>ok</span></div>
          <div class="count"><b>{summary['counts'].get('warn', 0)}</b><span>warn</span></div>
          <div class="count"><b>{summary['counts'].get('fail', 0)}</b><span>fail</span></div>
          <div class="count"><b>{summary['counts'].get('unknown', 0)}</b><span>unknown</span></div>
        </div>
      </section>
      <section class="panel incident">
        <h3>What needs attention</h3>
        <ul>{incident_rows}</ul>
      </section>
    </div>
  </header>
  <main>
    {grouped}

    <section class="playbook">
      <article class="panel">
        <h3>Email auth repair rule</h3>
        <ol>
          <li>Identify the failing profile and account first. Palmer and Echo credentials are separate.</li>
          <li>Smoke-test Gmail and Calendar for that exact account. Empty results are fine; auth errors are not.</li>
          <li>Remove and re-auth only the broken account token. Do not revoke every Google account unless the whole keyring is busted.</li>
          <li>After the browser/manual auth flow, verify with live Gmail + Calendar commands before declaring victory.</li>
        </ol>
      </article>
      <article class="panel">
        <h3>No-SSH repair center — next layer</h3>
        <ol>
          <li>This page already tells you exactly which account is broken.</li>
          <li>The next version should start the targeted `gog auth add --manual` flow from the browser.</li>
          <li>It should show the Google consent URL, accept the final localhost redirect URL, exchange it server-side, and rerun smoke tests.</li>
          <li>Credential mutation needs an explicit approval gate; OAuth codes/tokens must stay transient and out of notes/logs.</li>
        </ol>
      </article>
      <article class="panel">
        <h3>Refresh this artifact</h3>
        <pre>cd /Users/skylarpayne/artifactd
. .venv/bin/activate
python scripts/agent_health_dashboard.py --out dist/agent-health
artifactd --home /Users/skylarpayne/.hermes/artifacts --public-base-url https://artifacts.skylarbpayne.com deploy dist/agent-health --slug agent-health --title "Palmer / Echo connectivity health" --description "Protected dashboard with live smoke-test status for Palmer and Echo app connectivity, especially Google/email auth." --password "$(cat /Users/skylarpayne/.hermes/artifacts/.agent-health-password)" --pinned</pre>
      </article>
    </section>

    <section class="panel" style="margin-top:22px">
      <h3>Raw redacted snapshot</h3>
      <details><summary>Open JSON</summary><pre>{json_blob}</pre></details>
    </section>
    <p class="footer">Truth note: this artifact is a snapshot, not a daemon. If it gets reused, promote it to a scheduled refresh/checker and only ping Skylar on failures or re-auth-needed states.</p>
  </main>
</body>
</html>
"""


def write_outputs(checks: list[Check], out_dir: pathlib.Path, generated_at: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_html(checks, generated_at), encoding="utf-8")
    (out_dir / "health.json").write_text(
        json.dumps({"generated_at": generated_at, "summary": rollup(checks), "checks": [asdict(c) for c in checks]}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="dist/agent-health", help="Output directory containing index.html + health.json")
    parser.add_argument("--json", action="store_true", help="Print health JSON summary to stdout")
    args = parser.parse_args()

    generated_at = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    checks = collect_checks()
    write_outputs(checks, pathlib.Path(args.out), generated_at)
    result = {"generated_at": generated_at, "summary": rollup(checks), "out": str(pathlib.Path(args.out).resolve())}
    print(json.dumps(result, indent=2) if args.json else f"wrote {args.out} ({result['summary']['label']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
