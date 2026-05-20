from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def resolve_profile_home(profile: str, *, hermes_root: Optional[Path] = None, profile_home: Optional[Path] = None) -> Path:
    """Resolve the Hermes profile home without hardcoding Palmer paths.

    If HERMES_HOME is set, artifactd is already running inside a profile-scoped
    Hermes process, so that is the profile home. Otherwise a root Hermes home can
    be supplied and the profile lives under profiles/<name>.
    """

    _validate_profile_name(profile)
    if profile_home is not None:
        return Path(profile_home).expanduser()
    if hermes_root is not None:
        return Path(hermes_root).expanduser() / "profiles" / profile
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser()
    root = Path("~/.hermes").expanduser()
    return root / "profiles" / profile


def resolve_workspace_home(profile: str, *, hermes_root: Optional[Path] = None, profile_home: Optional[Path] = None) -> Path:
    return resolve_profile_home(profile, hermes_root=hermes_root, profile_home=profile_home) / "workspaces"


def _validate_profile_name(profile: str) -> None:
    if not _PROFILE_RE.fullmatch(profile or "") or ".." in profile:
        raise ValueError("profile must be a simple Hermes profile name, not a path")
