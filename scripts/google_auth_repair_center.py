#!/usr/bin/env python3
"""Generate the protected Google auth repair center artifact.

The artifact is intentionally safe by default. It renders a static health
snapshot plus a browser UI for a targeted `gog auth add --manual` flow. Live
token mutation still requires artifactd to serve the protected artifact with the
`gog.reauth` capability and a server-side approval flag/marker.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ARTIFACT_SLUG = "google-auth-repair-center"
REPAIR_CAPABILITY = "gog.reauth"
MUTATION_FLAG_ENV = "ARTIFACTD_ENABLE_GOG_REAUTH_ACTIONS"


@dataclass(frozen=True)
class AccountConfig:
    account: str
    label: str


@dataclass(frozen=True)
class ProfileConfig:
    profile: str
    owner_label: str
    profile_path: pathlib.Path
    artifact_home: pathlib.Path
    public_base_url: str
    accounts: tuple[AccountConfig, ...]


PROFILES: dict[str, ProfileConfig] = {
    "palmer": ProfileConfig(
        profile="palmer",
        owner_label="Palmer/Skylar",
        profile_path=pathlib.Path("/Users/skylarpayne/.hermes/profiles/palmer"),
        artifact_home=pathlib.Path("/Users/skylarpayne/.hermes/artifacts"),
        public_base_url="https://artifacts.skylarbpayne.com",
        accounts=(
            AccountConfig("skylar.b.payne@gmail.com", "Skylar personal Google"),
            AccountConfig("me@skylarbpayne.com", "Skylar domain Google"),
            AccountConfig("jacquelineandskylar@gmail.com", "Wedding Google"),
            AccountConfig("palmer@skylarbpayne.com", "Palmer Google"),
        ),
    ),
    "echo": ProfileConfig(
        profile="echo",
        owner_label="Echo/Jacqueline",
        profile_path=pathlib.Path("/Users/skylarpayne/.hermes/profiles/echo"),
        artifact_home=pathlib.Path("/Users/skylarpayne/.hermes/profiles/echo/artifacts"),
        public_base_url="https://artifacts.agoracomms.com",
        accounts=(
            AccountConfig("jacquelineaguilar030@gmail.com", "Jacqueline personal Google"),
            AccountConfig("jaguilar@y2lef.org", "Jacqueline Y2LEF Google"),
            AccountConfig("jacquelineandskylar@gmail.com", "Wedding Google"),
        ),
    ),
}


@dataclass
class ServiceCheck:
    account: str
    label: str
    profile: str
    service: str
    operation: str
    status: str
    summary: str
    evidence: str
    seconds: float


def load_profile_env(config: ProfileConfig) -> dict[str, str]:
    """Build the gog environment for exactly the selected Hermes profile.

    For Echo this sets HERMES_HOME, HOME, GOG_CONFIG_DIR, and loads only
    GOG_KEYRING_PASSWORD from Echo's .env; it never prints the value.
    """

    profile = config.profile_path
    env = os.environ.copy()
    env.pop("GOG_KEYRING_PASSWORD", None)
    env.update(
        {
            "HERMES_HOME": str(profile),
            "HOME": str(profile / "home"),
            "GOG_CONFIG_DIR": str(profile / "home/Library/Application Support/gogcli"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", ""),
        }
    )
    env_path = profile / ".env"
    if env_path.exists():
        for raw in env_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("GOG_KEYRING_PASSWORD="):
                env["GOG_KEYRING_PASSWORD"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return env


def run(cmd: list[str], env: dict[str, str], timeout: int = 30) -> tuple[int, str, str, float]:
    start = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - start
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timeout", time.time() - start
    except FileNotFoundError as exc:
        return 127, "", str(exc), time.time() - start


def parse_json(text: str) -> Any | None:
    try:
        return json.loads(text, strict=False)
    except Exception:
        return None


def redact(text: str, limit: int = 320) -> str:
    text = (text or "").replace("\x00", "")
    text = re.sub(r"https://accounts\.google\.com/[^\s\"'<>]+", "[GOOGLE_AUTH_URL_REDACTED]", text)
    text = re.sub(r"(?i)(code=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(state=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(refresh_token|access_token|client_secret|password)\"?\s*[:=]\s*\"?[^\"\s,}]+", r"\1=REDACTED", text)
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def legacy_backup_token_inventory_error(text: str) -> bool:
    """Return true when `gog auth list` is poisoned by old backup token files.

    Echo has historically carried keyring files like
    `token:...bak-YYYY...` and `token:...pre-reencrypt-YYYY...`. Individual
    account API calls can be healthy while `gog auth list` fails globally on
    those stale backups, so do not classify every account as needing reauth.
    """

    return bool(re.search(r"(?:token[: ]|token for ).*(?:\.bak-|\.pre-reencrypt-)", text or ""))


def smoke_account(env: dict[str, str], profile: str, account: str, label: str) -> list[ServiceCheck]:
    checks: list[ServiceCheck] = []
    specs = [
        (
            "Gmail",
            "search newer_than:7d",
            ["gog", "gmail", "search", "newer_than:7d", "--account", account, "--json", "--max=1", "--no-input"],
        ),
        (
            "Google Calendar",
            "events primary next 7d",
            ["gog", "calendar", "events", "primary", "--account", account, "--days=7", "--json", "--max=1", "--no-input"],
        ),
        (
            "Google Drive",
            "list root folder",
            ["gog", "drive", "ls", "--account", account, "--json", "--max=1", "--no-input"],
        ),
        (
            "OAuth service set",
            "gog auth list includes all supported services",
            ["gog", "auth", "list", "--json", "--no-input"],
        ),
    ]
    for service, operation, cmd in specs:
        rc, out, err, secs = run(cmd, env, 35)
        data = parse_json(out)
        if rc == 0 and data is not None:
            status = "ok"
            summary = f"{service} read smoke passed"
            if service == "Gmail" and isinstance(data, dict):
                threads = data.get("threads") or []
                evidence = threads[0].get("subject", "API returned no recent threads") if threads else "API returned no recent threads"
            elif service == "Google Calendar" and isinstance(data, dict):
                events = data.get("events") or []
                evidence = events[0].get("summary", "calendar reachable; no upcoming events returned") if events else "calendar reachable; no upcoming events returned"
            elif service == "Google Drive" and isinstance(data, dict):
                files = data.get("files") or data.get("items") or []
                evidence = files[0].get("name", "Drive reachable; no root files returned") if files else "Drive reachable; no root files returned"
            elif service == "OAuth service set" and isinstance(data, dict):
                required = {"gmail", "calendar", "drive", "docs", "sheets", "contacts", "tasks", "people"}
                accounts = data.get("accounts") or []
                entry = next((item for item in accounts if item.get("email") == account), {})
                services = set(entry.get("services") or [])
                missing = sorted(required - services)
                if missing:
                    status = "fail"
                    summary = "OAuth full-service inventory is incomplete"
                    evidence = "missing services: " + ", ".join(missing)
                else:
                    summary = "Full supported gog service set present"
                    evidence = "gmail, calendar, drive, docs, sheets, contacts, tasks, people"
            else:
                evidence = "API returned JSON"
        else:
            if service == "OAuth service set" and legacy_backup_token_inventory_error(err or out):
                status = "unknown"
                summary = "OAuth inventory blocked by legacy backup token"
                evidence = "gog auth list is blocked by an old backup token file; explicit Gmail, Calendar, and Drive checks still ran for this account"
            else:
                status = "fail"
                summary = f"{service} read smoke failed"
                evidence = redact(err or out)
        checks.append(
            ServiceCheck(
                account=account,
                label=label,
                profile=profile,
                service=service,
                operation=operation,
                status=status,
                summary=summary,
                evidence=evidence,
                seconds=secs,
            )
        )
    return checks


def static_account(profile: str, account: str, label: str) -> list[ServiceCheck]:
    specs = [
        ("Gmail", "search newer_than:7d"),
        ("Google Calendar", "events primary next 7d"),
        ("Google Drive", "list root folder"),
        ("OAuth service set", "gog auth list includes all supported services"),
    ]
    return [
        ServiceCheck(
            account=account,
            label=label,
            profile=profile,
            service=service,
            operation=operation,
            status="unknown",
            summary=f"{service} live smoke skipped",
            evidence="static generation smoke test; no gog command executed",
            seconds=0.0,
        )
        for service, operation in specs
    ]


def collect_checks(config: ProfileConfig, *, live: bool = True) -> list[ServiceCheck]:
    checks: list[ServiceCheck] = []
    env = load_profile_env(config) if live else {}
    for item in config.accounts:
        if live:
            checks.extend(smoke_account(env, config.profile, item.account, item.label))
        else:
            checks.extend(static_account(config.profile, item.account, item.label))
    return checks


def account_rollup(config: ProfileConfig, checks: list[ServiceCheck]) -> list[dict[str, Any]]:
    by_account: dict[str, list[ServiceCheck]] = {}
    for check in checks:
        by_account.setdefault(check.account, []).append(check)
    rows = []
    label_by_account = {item.account: item.label for item in config.accounts}
    for account in [item.account for item in config.accounts]:
        items = by_account.get(account, [])
        if items and all(c.status == "ok" for c in items):
            status = "ok"
        elif any(c.status == "fail" for c in items):
            status = "fail"
        else:
            status = "unknown"
        rows.append(
            {
                "account": account,
                "label": label_by_account[account],
                "profile": config.profile,
                "status": status,
                "checks": [asdict(c) for c in items],
            }
        )
    return rows


def overall(checks: list[ServiceCheck]) -> dict[str, Any]:
    counts = {"ok": 0, "fail": 0, "unknown": 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    broken_accounts = sorted({c.account for c in checks if c.status == "fail"})
    unknown_accounts = sorted({c.account for c in checks if c.status == "unknown"})
    if broken_accounts:
        status = "fail"
        label = "Targeted re-auth needed"
    elif unknown_accounts:
        status = "unknown"
        label = "Static snapshot generated; live smoke skipped"
    else:
        status = "ok"
        label = "Google auth healthy"
    return {
        "status": status,
        "label": label,
        "counts": counts,
        "broken_accounts": broken_accounts,
        "unknown_accounts": unknown_accounts,
    }


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def build_snapshot(config: ProfileConfig, checks: list[ServiceCheck], generated_at: str, *, live: bool) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "profile": config.profile,
        "owner_label": config.owner_label,
        "artifact_slug": ARTIFACT_SLUG,
        "artifact_home": str(config.artifact_home),
        "public_base_url": config.public_base_url,
        "live_smoke_executed": live,
        "summary": overall(checks),
        "accounts": account_rollup(config, checks),
        "checks": [asdict(c) for c in checks],
        "approval_boundary": {
            "required_capability": REPAIR_CAPABILITY,
            "required_server_flag": MUTATION_FLAG_ENV,
            "live_mutation_enabled_by_default": False,
            "note": "Browser-triggered OAuth token mutation requires protected artifact auth, artifact capability, and server approval marker/flag.",
        },
    }


def render_html(config: ProfileConfig, checks: list[ServiceCheck], generated_at: str, *, live: bool) -> str:
    snapshot = build_snapshot(config, checks, generated_at, live=live)
    summary = snapshot["summary"]
    accounts = snapshot["accounts"]
    json_blob = json.dumps(snapshot, indent=2)

    account_cards = "".join(
        f"""
        <article class="account {esc(row['status'])}" data-account="{esc(row['account'])}">
          <div class="account-top">
            <div><span class="dot {esc(row['status'])}"></span><strong>{esc(row['label'])}</strong><br><code>{esc(row['account'])}</code><br><small>profile: {esc(row['profile'])}</small></div>
            <span class="badge {esc(row['status'])}">{esc(row['status'].upper())}</span>
          </div>
          <div class="checks">
            {''.join(f'<div class="mini {esc(c["status"])}" data-service="{esc(c["service"])}"><b>{esc(c["service"])}</b><span>{esc(c["summary"])}</span><small>{esc(c["evidence"])}</small></div>' for c in row['checks'])}
          </div>
          <div class="repair-box">
            <p><strong>Full-scope targeted repair.</strong> This starts full supported gog auth (<code>--services all --drive-scope full</code>) for this one {esc(config.owner_label)} account in the <code>{esc(config.profile)}</code> profile. It will not touch accounts outside this artifact's allowlist.</p>
            <button type="button" class="start" data-account="{esc(row['account'])}">Start browser re-auth for this account</button>
            <div class="flow" id="flow-{esc(row['account'].replace('@','-at-').replace('.','-'))}"></div>
          </div>
        </article>
        """
        for row in accounts
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{esc(config.owner_label)} Google Auth Repair Center</title>
  <style>
    :root {{ color-scheme: dark; --bg:#070b12; --panel:#111827; --panel2:#0f172a; --text:#e5e7eb; --muted:#94a3b8; --line:rgba(148,163,184,.24); --ok:#22c55e; --fail:#ef4444; --unknown:#f59e0b; --accent:#60a5fa; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:radial-gradient(circle at top left,rgba(96,165,250,.18),transparent 34rem),var(--bg); color:var(--text); }}
    header, main {{ width:min(1160px,calc(100% - 32px)); margin:0 auto; }}
    header {{ padding:42px 0 22px; }}
    h1 {{ margin:10px 0 12px; font-size:clamp(34px,6vw,66px); letter-spacing:-.06em; line-height:.92; }}
    p {{ color:var(--muted); line-height:1.55; }}
    code, pre, textarea {{ font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace; }}
    .topline {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
    .badge {{ border:1px solid currentColor; border-radius:999px; padding:5px 10px; font-size:12px; font-weight:850; letter-spacing:.08em; }}
    .badge.ok {{ color:var(--ok); }} .badge.fail {{ color:var(--fail); }} .badge.unknown {{ color:var(--unknown); }}
    .hero {{ display:grid; grid-template-columns:1.15fr .85fr; gap:16px; margin-top:22px; }}
    .panel, .account {{ background:linear-gradient(180deg,rgba(17,24,39,.98),rgba(15,23,42,.95)); border:1px solid var(--line); border-radius:24px; padding:20px; box-shadow:0 24px 80px rgba(0,0,0,.32); }}
    .counts {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:18px; }}
    .count {{ background:rgba(2,6,23,.38); border:1px solid var(--line); border-radius:16px; padding:14px; }}
    .count b {{ display:block; font-size:28px; }} .count span {{ color:var(--muted); text-transform:uppercase; font-size:12px; letter-spacing:.08em; }}
    .accounts {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:20px; }}
    .account {{ border-left:5px solid var(--unknown); }} .account.ok {{ border-left-color:var(--ok); }} .account.fail {{ border-left-color:var(--fail); }}
    .account-top {{ display:flex; justify-content:space-between; align-items:start; gap:14px; }}
    .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px; background:var(--unknown); box-shadow:0 0 18px currentColor; }} .dot.ok {{ background:var(--ok); color:var(--ok); }} .dot.fail {{ background:var(--fail); color:var(--fail); }} .dot.unknown {{ background:var(--unknown); color:var(--unknown); }}
    .checks {{ display:grid; gap:8px; margin-top:14px; }}
    .mini {{ border:1px solid var(--line); border-radius:14px; padding:10px; background:rgba(2,6,23,.34); }}
    .mini b, .mini span, .mini small {{ display:block; }} .mini span {{ color:var(--text); margin-top:3px; }} .mini small {{ color:var(--muted); margin-top:4px; line-height:1.35; }}
    .repair-box {{ margin-top:14px; border:1px solid rgba(245,158,11,.35); background:rgba(245,158,11,.08); border-radius:16px; padding:14px; }}
    button {{ border:0; border-radius:999px; padding:12px 15px; background:var(--accent); color:#06101f; font-weight:850; cursor:pointer; }}
    button[disabled] {{ opacity:.55; cursor:not-allowed; }}
    textarea {{ width:100%; min-height:88px; resize:vertical; border:1px solid var(--line); border-radius:14px; padding:12px; color:var(--text); background:rgba(2,6,23,.65); }}
    a {{ color:#bfdbfe; }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:rgba(2,6,23,.75); border:1px solid var(--line); border-radius:16px; padding:14px; color:#d1fae5; }}
    .flow {{ margin-top:12px; display:grid; gap:10px; }}
    .notice {{ border:1px solid var(--line); border-radius:16px; padding:12px; background:rgba(2,6,23,.38); color:var(--muted); }}
    @media (max-width:850px) {{ .hero, .accounts {{ grid-template-columns:1fr; }} .counts {{ grid-template-columns:repeat(3,1fr); }} }}
  </style>
</head>
<body>
  <header>
    <div class="topline"><span class="badge {esc(summary['status'])}">{esc(summary['status'].upper())}</span><span>Snapshot generated {esc(generated_at)}</span><span>Protected artifact: /{ARTIFACT_SLUG}</span></div>
    <h1>{esc(config.owner_label)} Google auth repair center</h1>
    <p>This page shows the latest generated <code>{esc(config.profile)}</code> profile Gmail + Calendar + Drive smoke-test snapshot and a full supported OAuth service inventory for only this artifact's allowlisted accounts. Repair uses <code>gog auth add --services all --drive-scope full</code>. No blanket OAuth demolition. No sends. No calendar edits. No token/code persistence.</p>
    <div class="hero">
      <section class="panel">
        <h2>{esc(summary['label'])}</h2>
        <p><strong>Overall status is across this artifact's allowlisted accounts only.</strong> Duplicate emails, such as the wedding Gmail, resolve to the current artifactd profile: <code>{esc(config.profile)}</code>.</p>
        <p>Broken accounts in this snapshot: {esc(', '.join(summary['broken_accounts']) or 'none')}</p>
        <div class="counts">
          <div class="count"><b>{summary['counts'].get('ok', 0)}</b><span>ok checks</span></div>
          <div class="count"><b>{summary['counts'].get('fail', 0)}</b><span>failed checks</span></div>
          <div class="count"><b>{summary['counts'].get('unknown', 0)}</b><span>unknown checks</span></div>
        </div>
      </section>
      <section class="panel">
        <h2>Approval boundary</h2>
        <p>The UI and static status page are safe to publish behind artifact password auth. Live browser-triggered OAuth mutation still requires:</p>
        <ul>
          <li>Protected artifact auth required.</li>
          <li>Artifact must be deployed with <code>{REPAIR_CAPABILITY}</code>.</li>
          <li>Server must have <code>{MUTATION_FLAG_ENV}=1</code> or the approved marker file present.</li>
          <li>Audit logs store action/result metadata only; codes, tokens, auth URLs, and client secrets are redacted or transient.</li>
        </ul>
      </section>
    </div>
  </header>
  <main>
    <section class="accounts">
      {account_cards}
    </section>
    <section class="panel" style="margin-top:20px">
      <h2>Raw redacted snapshot</h2>
      <details><summary>Open JSON</summary><pre id="snapshot-json">{esc(json_blob)}</pre></details>
    </section>
  </main>
<script id="repair-snapshot" type="application/json">{esc(json.dumps(snapshot))}</script>
<script>
const slug = {json.dumps(ARTIFACT_SLUG)};
const mutationFlag = {json.dumps(MUTATION_FLAG_ENV)};
const capability = {json.dumps(REPAIR_CAPABILITY)};
function flowId(account) {{ return 'flow-' + account.replace('@','-at-').replaceAll('.','-'); }}
function renderFlow(account, html) {{ document.getElementById(flowId(account)).innerHTML = html; }}
function serviceLabel(service) {{
  return {{gmail:'Gmail', calendar:'Google Calendar', drive:'Google Drive', 'oauth-scopes':'OAuth service set'}}[service] || service;
}}
function serviceSummary(service, ok) {{
  if (service === 'oauth-scopes') return ok ? 'Full supported gog service set present' : 'OAuth full-service inventory is incomplete';
  return `${{serviceLabel(service)}} read smoke ${{ok ? 'passed' : 'failed'}}`;
}}
function accountCard(account) {{ return Array.from(document.querySelectorAll('.account')).find(el => el.dataset.account === account); }}
function updateAccountFromVerification(account, verification) {{
  if (!verification || !Array.isArray(verification.checks)) return;
  const card = accountCard(account);
  if (!card) return;
  const ok = !!verification.ok;
  card.classList.remove('ok', 'fail', 'unknown');
  card.classList.add(ok ? 'ok' : 'fail');
  const dot = card.querySelector('.dot');
  if (dot) {{ dot.classList.remove('ok', 'fail', 'unknown'); dot.classList.add(ok ? 'ok' : 'fail'); }}
  const badge = card.querySelector('.account-top .badge');
  if (badge) {{ badge.classList.remove('ok', 'fail', 'unknown'); badge.classList.add(ok ? 'ok' : 'fail'); badge.textContent = ok ? 'OK' : 'FAIL'; }}
  for (const check of verification.checks) {{
    const label = serviceLabel(check.service);
    const mini = Array.from(card.querySelectorAll('.mini')).find(el => el.dataset.service === label);
    if (!mini) continue;
    const checkOk = !!check.ok;
    mini.classList.remove('ok', 'fail', 'unknown');
    mini.classList.add(checkOk ? 'ok' : 'fail');
    const summary = mini.querySelector('span');
    const evidence = mini.querySelector('small');
    if (summary) summary.textContent = serviceSummary(check.service, checkOk);
    if (evidence) evidence.textContent = check.evidence || (checkOk ? 'validated after re-auth' : 'failed after re-auth');
  }}
}}
function errorDetailMessage(payload, fallback) {{
  const detail = payload && payload.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item && typeof item === 'object' ? ((Array.isArray(item.loc) ? item.loc.join('.') + ': ' : '') + (item.msg || JSON.stringify(item))) : String(item)).join('; ');
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return fallback;
}}
async function postForm(path, data) {{
  const body = new URLSearchParams(data);
  const res = await fetch(path, {{ method:'POST', headers: {{'Content-Type':'application/x-www-form-urlencoded'}}, body }});
  const text = await res.text();
  let payload;
  try {{ payload = JSON.parse(text); }} catch {{ payload = {{detail:text}}; }}
  if (!res.ok) throw Object.assign(new Error(errorDetailMessage(payload, res.statusText)), {{status:res.status, payload}});
  return payload;
}}
function renderSession(account, payload) {{
  let parts = [`<div class="notice"><strong>Status:</strong> ${{payload.status}}</div>`];
  if (payload.auth_url) {{
    parts.push(`<div class="notice"><strong>Step 1:</strong> open Google consent URL:<br><a href="${{payload.auth_url}}" target="_blank" rel="noopener">Open Google consent for ${{account}}</a></div>`);
    parts.push(`<div class="notice"><strong>Step 2:</strong> after Google redirects to a dead localhost page, copy the entire address bar URL and paste it below.</div>`);
    parts.push(`<textarea placeholder="http://localhost:1/?code=..." id="redirect-${{flowId(account)}}"></textarea><button type="button" onclick="submitRedirect('${{account}}')">Submit localhost redirect URL</button>`);
  }} else {{
    parts.push(`<div class="notice">Waiting for gog to print the Google consent URL. Refresh status in a few seconds.</div>`);
  }}
  if (payload.verification) {{ updateAccountFromVerification(account, payload.verification); parts.push(`<pre>${{JSON.stringify(payload.verification, null, 2)}}</pre>`); }}
  if (payload.error) parts.push(`<div class="notice"><strong>Error:</strong> ${{payload.error}}</div>`);
  renderFlow(account, parts.join(''));
}}
async function startRepair(account) {{
  renderFlow(account, '<div class="notice">Starting targeted repair…</div>');
  try {{
    const payload = await postForm(`/${{slug}}/_interactive/gog/start`, {{account, force:'true'}});
    renderSession(account, payload);
    if (['starting','auth_url_ready','waiting_for_redirect'].includes(payload.status)) setTimeout(() => pollStatus(account), 1500);
  }} catch (err) {{
    const msg = err.status === 403
      ? `Live repair is not enabled yet. Approval/deploy needed: add artifact capability <code>${{capability}}</code> and set server approval marker/flag <code>${{mutationFlag}}=1</code>. Server said: ${{err.message}}`
      : err.message;
    renderFlow(account, `<div class="notice"><strong>Blocked:</strong> ${{msg}}</div>`);
  }}
}}
async function submitRedirect(account) {{
  const value = document.getElementById('redirect-' + flowId(account)).value;
  renderFlow(account, '<div class="notice">Submitting redirect transiently and verifying Gmail + Calendar + Drive + full service inventory…</div>');
  try {{
    const payload = await postForm(`/${{slug}}/_interactive/gog/submit`, {{account, redirect_url:value}});
    renderSession(account, payload);
    setTimeout(() => pollStatus(account), 3000);
  }} catch (err) {{ renderFlow(account, `<div class="notice"><strong>Submit failed:</strong> ${{err.message}}</div>`); }}
}}
async function pollStatus(account) {{
  const res = await fetch(`/${{slug}}/_interactive/gog/status?account=${{encodeURIComponent(account)}}`);
  const payload = await res.json();
  renderSession(account, payload);
  if (['submitted','verifying','auth_url_ready','waiting_for_redirect'].includes(payload.status)) setTimeout(() => pollStatus(account), 3000);
}}
document.querySelectorAll('button.start').forEach(btn => btn.addEventListener('click', () => startRepair(btn.dataset.account)));
</script>
</body>
</html>
"""


def write_outputs(config: ProfileConfig, checks: list[ServiceCheck], out_dir: pathlib.Path, generated_at: str, *, live: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(config, checks, generated_at, live=live)
    (out_dir / "health.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (out_dir / "index.html").write_text(render_html(config, checks, generated_at, live=live), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), default="palmer", help="Artifact/profile allowlist to generate")
    parser.add_argument("--out", default="dist/google-auth-repair-center", help="Output directory containing index.html + health.json")
    parser.add_argument("--json", action="store_true", help="Print summary JSON to stdout")
    parser.add_argument("--skip-live-checks", action="store_true", help="Generate static files without executing gog smoke checks")
    args = parser.parse_args()

    config = PROFILES[args.profile]
    generated_at = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    live = not args.skip_live_checks
    checks = collect_checks(config, live=live)
    out = pathlib.Path(args.out)
    write_outputs(config, checks, out, generated_at, live=live)
    result = {
        "generated_at": generated_at,
        "profile": config.profile,
        "summary": overall(checks),
        "out": str(out.resolve()),
        "accounts": [item.account for item in config.accounts],
        "live_smoke_executed": live,
    }
    print(json.dumps(result, indent=2) if args.json else f"wrote {out} ({result['summary']['label']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
