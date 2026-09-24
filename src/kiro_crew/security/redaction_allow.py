"""Hosts a reader allowed long queries for, one workspace at a time.

The dashboard's *Allow for this host* writes here after the reader confirms.
An allowed host skips ONLY the query-length and base64 heuristics of the
exfiltration check (the same relaxation the platform's exact-host exemption
gives); every credential check still runs, so an allowed host never lets a
secret through.

The file lives in the ``trust`` directory, which the agent can neither read
nor write (it is the SEL trust root, gated as a whole). An agent able to edit
this list could allow the host it wants to send conversation data to, so only
the dashboard's own routes write it. Every failure to read degrades to "no
host allowed", which means more redaction, the safe direction.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from kiro_crew.config.paths import config_dir

_LOCK = threading.Lock()
_HOST_RE = re.compile(
    r"^(?:[a-z0-9._-]{1,253}\.[a-z]{2,63}|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:.]{1,45}\])$"
)
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,128}$")
MAX_HOSTS_PER_WORKSPACE = 200
DEFAULT_WORKSPACE = "default"

_path_override: Path | None = None


def _path() -> Path:
    if _path_override is not None:
        return _path_override
    return config_dir() / "trust" / "redaction-allow.json"


def normalize_workspace(workspace: str | None) -> str:
    """The key a workspace is stored under; unknown shapes map to the default."""
    ws = (workspace or "").strip()
    return ws if _WORKSPACE_RE.match(ws) else DEFAULT_WORKSPACE


def valid_host(host: object) -> bool:
    return isinstance(host, str) and bool(_HOST_RE.match(host))


def _read() -> dict[str, list[str]]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for ws, hosts in raw.items():
        if isinstance(ws, str) and _WORKSPACE_RE.match(ws) and isinstance(hosts, list):
            kept = [h for h in hosts if valid_host(h)][:MAX_HOSTS_PER_WORKSPACE]
            if kept:
                out[ws] = kept
    return out


def _write(data: dict[str, list[str]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def allowed_hosts_for(workspace: str | None) -> frozenset[str]:
    """The hosts allowed in ``workspace``; empty on any read failure."""
    return frozenset(_read().get(normalize_workspace(workspace), []))


def list_allowed() -> dict[str, list[str]]:
    return _read()


def allow_host(workspace: str | None, host: str) -> bool:
    """Allow ``host`` in ``workspace``. False when the host is off-shape or the list is full."""
    host = host.lower()
    if not valid_host(host):
        return False
    ws = normalize_workspace(workspace)
    with _LOCK:
        data = _read()
        hosts = data.setdefault(ws, [])
        if host in hosts:
            return True
        if len(hosts) >= MAX_HOSTS_PER_WORKSPACE:
            return False
        hosts.append(host)
        _write(data)
    return True


def revoke_host(workspace: str | None, host: str) -> bool:
    """Remove ``host`` from ``workspace``. True when it was there."""
    host = host.lower()
    ws = normalize_workspace(workspace)
    with _LOCK:
        data = _read()
        hosts = data.get(ws, [])
        if host not in hosts:
            return False
        hosts.remove(host)
        if not hosts:
            data.pop(ws, None)
        _write(data)
    return True
