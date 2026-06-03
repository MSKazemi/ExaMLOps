from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "examlops" / "config.toml"

_DEFAULTS = {
    "control_plane_url": "http://localhost:18002",
    "ray_serve_url":     "http://localhost:18001",
    "mlflow_url":        "http://localhost:15000",
    "prefect_url":       "http://localhost:14200",
    "dashboard_url":     "http://localhost:18099",
    "control_plane_token": "",
    "dashboard_token":   "",
}

@dataclass
class Config:
    control_plane_url:   str = "http://localhost:18002"
    ray_serve_url:       str = "http://localhost:18001"
    mlflow_url:          str = "http://localhost:15000"
    prefect_url:         str = "http://localhost:14200"
    dashboard_url:       str = "http://localhost:18099"
    control_plane_token: str = ""
    dashboard_token:     str = ""

def load_config() -> Config:
    data: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            raw = tomllib.load(f)
        data.update(raw.get("urls", {}))
        data.update(raw.get("auth", {}))

    def _r(toml_key: str, env_key: str, default: str) -> str:
        return os.getenv(env_key) or data.get(toml_key) or default

    return Config(
        control_plane_url   = _r("control_plane",       "CONTROL_PLANE_URL",   _DEFAULTS["control_plane_url"]),
        ray_serve_url       = _r("ray_serve",           "RAY_SERVE_URL",       _DEFAULTS["ray_serve_url"]),
        mlflow_url          = _r("mlflow",              "MLFLOW_TRACKING_URI", _DEFAULTS["mlflow_url"]),
        prefect_url         = _r("prefect",             "PREFECT_API_URL",     _DEFAULTS["prefect_url"]),
        dashboard_url       = _r("dashboard",           "DASHBOARD_URL",       _DEFAULTS["dashboard_url"]),
        control_plane_token = _r("control_plane_token", "CONTROL_PLANE_TOKEN", _DEFAULTS["control_plane_token"]),
        dashboard_token     = _r("dashboard_token",     "DASHBOARD_TOKEN",     _DEFAULTS["dashboard_token"]),
    )

def write_config(updates: dict) -> None:
    """Merge updates into the config TOML file."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            existing = tomllib.load(f)

    _URL_KEYS = {"control_plane", "ray_serve", "mlflow", "prefect", "dashboard"}
    urls = existing.get("urls", {})
    auth = existing.get("auth", {})
    for k, v in updates.items():
        if k in _URL_KEYS:
            urls[k] = v
        else:
            auth[k] = v
    existing["urls"] = urls
    existing["auth"] = auth

    try:
        import tomli_w
        CONFIG_PATH.write_text(tomli_w.dumps(existing))
    except ImportError:
        lines = []
        for section, d in existing.items():
            lines.append(f"[{section}]")
            for key, val in d.items():
                lines.append(f'{key} = "{val}"')
            lines.append("")
        CONFIG_PATH.write_text("\n".join(lines))
