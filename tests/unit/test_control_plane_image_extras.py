"""The control-plane image must carry every backend the platform can be configured to use.

`EXAMLOPS_EVENT_PUBLISHER=nats` is what the Compose `events` profile and ADR 0124 document, but the
image was built with `[coordination,postgres,oidc]` and not `[events]`, so the NATS backbone could
not publish at all in a released image: every relay cycle logged "needs the 'nats-py' package" and
events stayed in the outbox. A chaos drill found it
(`tests/integration/test_backbone_outage_drill_live.py`).

The rule is mechanical, so the next backend cannot be forgotten: whenever the platform's code tells
an operator to `install 'examlops[<extra>]'` for a publisher or coordinator, the image installs it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "platform" / "services" / "control_plane" / "Dockerfile"
# Where an operator's choice of backend is made: a publisher (ADR 0124) or a coordinator.
BACKEND_SOURCES = (
    ROOT / "platform" / "cli" / "src" / "examlops" / "events",
    ROOT / "platform" / "cli" / "src" / "examlops" / "coordination",
)
_EXTRA = re.compile(r"examlops\[([a-z0-9,\-]+)\]")


def _installed_extras() -> set[str]:
    """The extras the image installs for the examlops package."""
    found = re.search(r"pip install -e '/app/platform/cli\[([^\]]+)\]'", DOCKERFILE.read_text())
    assert found, "the control-plane Dockerfile no longer installs the examlops package with extras"
    return {part.strip() for part in found.group(1).split(",")}


def _required_extras() -> dict[str, set[str]]:
    """Extras the backend code tells operators to install, by the file that names them."""
    required: dict[str, set[str]] = {}
    for source in BACKEND_SOURCES:
        paths = sorted(source.rglob("*.py")) if source.is_dir() else [source]
        for path in paths:
            for match in _EXTRA.finditer(path.read_text(encoding="utf-8")):
                for extra in match.group(1).split(","):
                    required.setdefault(str(path.relative_to(ROOT)), set()).add(extra.strip())
    return required


def test_there_are_backends_to_check():
    """Otherwise the guard below passes by inspecting nothing."""
    required = _required_extras()
    assert required, "no backend names an examlops extra any more — has the packaging changed?"
    assert "events" in {extra for extras in required.values() for extra in extras}


def test_the_image_installs_every_extra_a_backend_asks_for():
    installed = _installed_extras()
    missing = {
        path: sorted(extras - installed)
        for path, extras in _required_extras().items()
        if extras - installed
    }
    assert not missing, (
        "the control-plane image cannot use a backend the platform can be configured to use; "
        f"add the extra to its Dockerfile: {missing}"
    )


def test_every_extra_the_image_installs_exists():
    """A typo in the extras list installs nothing and fails nothing, until a backend is selected."""
    pyproject = tomllib.loads((ROOT / "platform" / "cli" / "pyproject.toml").read_text())
    declared = set(pyproject["project"]["optional-dependencies"])
    assert _installed_extras() <= declared, _installed_extras() - declared
