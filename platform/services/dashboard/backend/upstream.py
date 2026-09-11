"""How a backing service's HTTP status becomes the dashboard's own response status.

The dashboard calls the control plane, MLflow, Ray Serve and Grafana with *its own* service
credentials. When one of them answers 401/403/407 it is refusing **the dashboard**, not the person
in the browser — but forwarded verbatim it reads as "your session is invalid", and the SPA's
`apiFetch` clears the login and reloads on exactly those codes. With a mis-set `CONTROL_PLANE_TOKEN`
that signed an admin out on every page load (the approvals badge polls on every page). Such an
answer is a bad gateway: the upstream refused us. Every other status keeps its meaning.
"""

from __future__ import annotations

_CREDENTIAL_REFUSED = frozenset({401, 403, 407})


def dashboard_status(upstream_status: int) -> int:
    """The status to return for an upstream answer (credential refusals become 502)."""
    return 502 if upstream_status in _CREDENTIAL_REFUSED else upstream_status


def upstream_detail(service: str, upstream_status: int) -> str:
    """A browser-safe message: never the upstream body, but say *which* credential to fix."""
    if upstream_status in _CREDENTIAL_REFUSED:
        return (
            f"{service} refused the dashboard's service credential (upstream {upstream_status}) "
            "— check the token configured for it"
        )
    return f"{service} returned an error"
