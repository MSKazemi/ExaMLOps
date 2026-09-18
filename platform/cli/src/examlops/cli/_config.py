from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "examlops" / "config.toml"


def config_path() -> Path:
    """The effective config file path.

    ``EXAMLOPS_CONFIG`` overrides the default ``~/.config/examlops/config.toml`` so
    containers/CI can point at a pinned config and tests can run hermetically (never
    reading the developer's real config). Falls back to :data:`CONFIG_PATH`."""
    override = os.getenv("EXAMLOPS_CONFIG")
    return Path(override) if override else CONFIG_PATH


# field name, TOML key, env var, default, is_secret
_FIELDS: list[tuple[str, str, str, str, bool]] = [
    ("control_plane_url", "control_plane", "CONTROL_PLANE_URL", "http://localhost:18002", False),
    ("ray_serve_url", "ray_serve", "RAY_SERVE_URL", "http://localhost:18001", False),
    ("mlflow_url", "mlflow", "MLFLOW_TRACKING_URI", "http://localhost:15000", False),
    ("prefect_url", "prefect", "PREFECT_API_URL", "http://localhost:14200", False),
    ("dashboard_url", "dashboard", "DASHBOARD_URL", "http://localhost:18099", False),
    ("agent_url", "agent", "AGENT_URL", "http://localhost:18004", False),
    ("dataplane_url", "dataplane", "EXAMLOPS_DATAPLANE_URL", "http://localhost:18010", False),
    # The bridge is reached at the *host* port by anything running outside its container.
    # `exa seanerbus status` read a `seanerbus_bridge_url` attribute that no Config ever
    # had, and `exa production` hard-coded the address twice, so the variable the platform
    # documents for locating the bridge steered neither of them.
    (
        "seanerbus_bridge_url",
        "seanerbus_bridge",
        "SEANERBUS_BRIDGE_STATUS_URL",
        "http://localhost:18003",
        False,
    ),
    ("control_plane_token", "control_plane_token", "CONTROL_PLANE_TOKEN", "", True),
    ("dashboard_token", "dashboard_token", "DASHBOARD_TOKEN", "", True),
    ("agent_token", "agent_token", "AGENT_API_KEY", "", True),
    # Bearer for Ray Serve's admin routes (`exa serve reload`, the traffic-rule push). Unset on the
    # server closes those routes; see serving/admin_auth.py (plan P0.6).
    ("ray_serve_admin_token", "ray_serve_admin_token", "RAY_SERVE_ADMIN_TOKEN", "", True),
    # Bearer for inference when `ray_serve` is the serving gateway (ADR 0126): a virtual key
    # (`exa gateway key issue`) or an IdP access token. Sent only to URLs under `ray_serve`.
    ("serving_token", "serving_token", "EXAMLOPS_SERVING_TOKEN", "", True),
    ("dataplane_token", "dataplane_token", "EXAMLOPS_DATAPLANE_TOKEN", "", True),
    # Organisation sign-in (ADR 0120): the IdP `exa auth login` uses when no --provider/--issuer is
    # given, and the CLI's public OAuth client id there. Per context, so each site keeps its own.
    ("auth_issuer", "auth_issuer", "EXAMLOPS_AUTH_ISSUER", "", False),
    ("auth_client_id", "auth_client_id", "EXAMLOPS_AUTH_CLIENT_ID", "", False),
]

_URL_KEYS = {
    "auth_issuer",
    "control_plane",
    "ray_serve",
    "mlflow",
    "prefect",
    "dashboard",
    "agent",
    "seanerbus_bridge",
    "dataplane",
}

# Kept for backward compatibility with callers importing _DEFAULTS.
_DEFAULTS = {toml_key: default for _, toml_key, _, default, _ in _FIELDS}

# Every key the resolver actually reads, and the field names `exa config show`/`exa env` print
# (`mlflow_url` → `mlflow`). Writing any other key used to succeed and change nothing.
_TOML_KEYS = frozenset(toml_key for _, toml_key, _, _, _ in _FIELDS)
_ALIASES = {field: toml_key for field, toml_key, _, _, _ in _FIELDS}


class UnknownConfigKey(ValueError):
    """A config key nothing reads — refused rather than silently written."""


class InvalidConfigValue(ValueError):
    """A value the key cannot use (a URL key given something that is not an http(s) URL)."""


def check_value(toml_key: str, value: object) -> None:
    """Refuse a value the resolver's consumers cannot use — at write time, not at first request."""
    if toml_key in _URL_KEYS:
        from urllib.parse import urlparse

        parsed = urlparse(str(value))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise InvalidConfigValue(
                f"{toml_key} must be an http(s) URL with a host, e.g. http://host:port — got {value!r}"
            )


def file_findings() -> list[tuple[str, str]]:
    """``(where, message)`` for anything in the config file that nothing will read.

    Keys written before keys were validated (or by hand) stay in the file doing nothing, and an
    ``active_context`` naming a context that no longer exists silently means "base config".
    """
    raw = _read_raw()
    found: list[tuple[str, str]] = []

    def scan(section: dict, where: str) -> None:
        for table in ("urls", "auth"):
            for key in section.get(table, {}) or {}:
                try:
                    canonical_key(key)
                except UnknownConfigKey as exc:
                    hint = str(exc).split(" Valid keys:")[0]
                    found.append((f"{where}.{table}.{key}", f"{hint} (in {where} [{table}])"))

    scan(raw, "base")
    for name, section in (raw.get("contexts") or {}).items():
        scan(section or {}, f"context {name}")
    active = raw.get("active_context")
    if active and active not in (raw.get("contexts") or {}):
        found.append(
            (
                "active_context",
                f"active context {active!r} does not exist — commands use the base config; "
                "run `exa config use --clear` or create it",
            )
        )
    return found


def canonical_key(key: str) -> str:
    """The TOML key for ``key`` (a TOML key or a field name); raise :class:`UnknownConfigKey`."""
    k = key.strip().lower().replace("-", "_")
    if k in _TOML_KEYS:
        return k
    if k in _ALIASES:
        return _ALIASES[k]
    import difflib

    # A fragment of a real key ("token", "control") means every key it is part of; a typo
    # ("mlfow") means the nearest spellings. Fragments first — they are the likelier intent.
    fragment = sorted(t for t in _TOML_KEYS if t.endswith(f"_{k}") or t.startswith(f"{k}_"))
    fuzzy = difflib.get_close_matches(k, sorted(_TOML_KEYS | set(_ALIASES)), n=3, cutoff=0.5)
    close = list(dict.fromkeys([*fragment, *fuzzy]))[:4]
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise UnknownConfigKey(
        f"unknown config key {key!r} — nothing reads it.{hint} "
        f"Valid keys: {', '.join(sorted(_TOML_KEYS))}."
    )


@dataclass
class Config:
    control_plane_url: str = "http://localhost:18002"
    ray_serve_url: str = "http://localhost:18001"
    mlflow_url: str = "http://localhost:15000"
    prefect_url: str = "http://localhost:14200"
    dashboard_url: str = "http://localhost:18099"
    agent_url: str = "http://localhost:18004"
    dataplane_url: str = "http://localhost:18010"
    seanerbus_bridge_url: str = "http://localhost:18003"
    control_plane_token: str = ""
    dashboard_token: str = ""
    agent_token: str = ""
    ray_serve_admin_token: str = ""
    serving_token: str = ""
    dataplane_token: str = ""
    auth_issuer: str = ""
    auth_client_id: str = ""


def _read_raw() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def active_context(raw: dict | None = None) -> str | None:
    """The active context name: ``EXAMLOPS_CONTEXT`` env wins, else the TOML pointer."""
    env = os.getenv("EXAMLOPS_CONTEXT")
    if env:
        return env
    if raw is None:
        raw = _read_raw()
    return raw.get("active_context")


def _merged_file_data(raw: dict) -> dict:
    """Merge legacy top-level [urls]/[auth] with the active context overlay."""
    data: dict = {}
    data.update(raw.get("urls", {}))
    data.update(raw.get("auth", {}))
    ctx = active_context(raw)
    if ctx:
        section = raw.get("contexts", {}).get(ctx, {})
        data.update(section.get("urls", {}))
        data.update(section.get("auth", {}))
    return data


def load_config() -> Config:
    data = _merged_file_data(_read_raw())
    values: dict[str, str] = {}
    for field, toml_key, env_key, default, _ in _FIELDS:
        values[field] = os.getenv(env_key) or data.get(toml_key) or default
    # A token file (CONTROL_PLANE_TOKEN_FILE: a workload identity spiffe-helper keeps fresh, or a
    # rotated Secret) wins over the static value; the file is re-read each time config is loaded.
    from examlops.service_auth import control_plane_bearer  # noqa: PLC0415

    values["control_plane_token"] = control_plane_bearer(values["control_plane_token"])
    if not values["control_plane_token"] or not values["dashboard_token"]:
        # Signed in with `exa auth login` (ADR 0120)? Then calls carry the user's own identity.
        # An explicitly configured static token always wins; no session means no change.
        session_token = _session_token()
        values["control_plane_token"] = values["control_plane_token"] or session_token
        values["dashboard_token"] = values["dashboard_token"] or session_token
    return Config(**values)


def _session_token() -> str:
    try:
        from examlops.iam.session import current_access_token

        return current_access_token()
    except Exception:  # noqa: BLE001 — a broken session must never break an unrelated command
        return ""


def resolve_with_provenance() -> list[dict]:
    """Return each config field with its resolved value and where it came from.

    Source is one of ``env:<VAR>``, ``context:<name>``, ``file``, or ``default``.
    Secret values are redacted.
    """
    raw = _read_raw()
    ctx = active_context(raw)
    legacy = {**raw.get("urls", {}), **raw.get("auth", {})}
    ctx_section = raw.get("contexts", {}).get(ctx, {}) if ctx else {}
    ctx_data = {**ctx_section.get("urls", {}), **ctx_section.get("auth", {})}

    out = []
    for field, toml_key, env_key, default, is_secret in _FIELDS:
        env_val = os.getenv(env_key)
        if env_val:
            value, source = env_val, f"env:{env_key}"
        elif toml_key in ctx_data:
            value, source = ctx_data[toml_key], f"context:{ctx}"
        elif toml_key in legacy:
            value, source = legacy[toml_key], "file"
        else:
            value, source = default, "default"
        display = ("***" if value else "(unset)") if is_secret else (value or "(unset)")
        out.append({"key": field, "value": display, "source": source})
    return out


def list_contexts() -> tuple[list[str], str | None]:
    raw = _read_raw()
    return sorted(raw.get("contexts", {}).keys()), active_context(raw)


def set_active_context(name: str) -> None:
    """Point ``active_context`` at ``name`` (creating an empty context if new)."""
    existing = _read_raw()
    existing.setdefault("contexts", {}).setdefault(name, {})
    existing["active_context"] = name
    _write_raw(existing)


def active_project(raw: dict | None = None) -> str | None:
    """The active Project (ADR 0086): ``EXAMLOPS_PROJECT`` env wins, else the TOML pointer."""
    env = os.getenv("EXAMLOPS_PROJECT")
    if env:
        return env
    if raw is None:
        raw = _read_raw()
    return raw.get("active_project")


def scoped_agent_session(session_id: str, raw: dict | None = None) -> str:
    """Namespace an agent checkpoint by the active ExaMLOps project."""
    project = active_project(raw)
    return f"{project}:{session_id}" if project else session_id


def set_active_project(name: str | None) -> None:
    """Persist ``active_project`` in config.toml (``None`` clears it)."""
    existing = _read_raw()
    if name:
        existing["active_project"] = name
    else:
        existing.pop("active_project", None)
    _write_raw(existing)


def write_config(updates: dict, context: str | None = None) -> None:
    """Merge updates into the config TOML file.

    When ``context`` is given, updates are written into ``[contexts.<name>.urls|auth]``;
    otherwise into the legacy top-level ``[urls]``/``[auth]`` sections.
    """
    # Validate every key and value before touching the file, so a bad one writes nothing at all.
    updates = {canonical_key(k): v for k, v in updates.items()}
    for k, v in updates.items():
        check_value(k, v)
    existing = _read_raw()

    if context:
        contexts = existing.setdefault("contexts", {})
        target = contexts.setdefault(context, {})
    else:
        target = existing

    urls = target.get("urls", {})
    auth = target.get("auth", {})
    for k, v in updates.items():
        if k in _URL_KEYS:
            urls[k] = v
        else:
            auth[k] = v
    target["urls"] = urls
    target["auth"] = auth
    _write_raw(existing)


def unset_config(key: str, context: str | None = None) -> bool:
    """Remove ``key`` from the base config or from ``context``; the value falls back to the next
    source (the base config, then the default). Returns whether anything was removed."""
    k = canonical_key(key)
    existing = _read_raw()
    target = existing.get("contexts", {}).get(context, {}) if context else existing
    section = target.get("urls" if k in _URL_KEYS else "auth", {})
    if k not in section:
        return False
    del section[k]
    _write_raw(existing)
    return True


def delete_context(name: str) -> bool:
    """Remove a context (and the active pointer, when it pointed there). False if absent."""
    existing = _read_raw()
    contexts = existing.get("contexts", {})
    if name not in contexts:
        return False
    del contexts[name]
    if not contexts:
        existing.pop("contexts", None)
    if existing.get("active_context") == name:
        existing.pop("active_context", None)
    _write_raw(existing)
    return True


def clear_active_context() -> None:
    """Return to the base configuration (no context overlay)."""
    existing = _read_raw()
    if existing.pop("active_context", None) is not None:
        _write_raw(existing)


def _write_raw(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import tomli_w

        path.write_text(tomli_w.dumps(data))
    except ImportError:
        path.write_text(_dumps_toml(data))
    # Config may contain bearer tokens. Do not depend on the caller's umask.
    path.chmod(0o600)


def _toml_value(val: object) -> str:
    """Serialise a scalar as a valid TOML value (fallback path only)."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    # TOML basic string: escape backslash and double-quote (and control chars)
    # so tokens/paths/regexes containing " or \ round-trip through tomllib.
    s = str(val)
    escaped = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _dumps_toml(data: dict, prefix: str = "") -> str:
    """Minimal TOML writer (fallback when tomli_w is unavailable)."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, val in scalars.items():
        lines.append(f"{key} = {_toml_value(val)}")
    if scalars:
        lines.append("")
    for key, val in tables.items():
        section = f"{prefix}{key}"
        body = _dumps_toml(val, prefix=f"{section}.")
        # Only emit a [section] header for tables that hold scalars directly.
        if any(not isinstance(v, dict) for v in val.values()):
            lines.append(f"[{section}]")
        lines.append(body)
    return "\n".join(lines)
