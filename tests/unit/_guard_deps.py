"""Make a repository guard say *why* it could not run.

Seven guards in this directory answer questions about the repository itself — what the
public tree tracks, whether every environment variable is documented, whether each tag
yields release notes — by shelling out to ``git`` or ``make``. When the binary is absent
they do not fail an assertion; they raise ``FileNotFoundError`` from deep inside
``subprocess``, which reads as a broken test rather than as an unprotected tree.

That is not hypothetical. ``python:3.12-slim`` ships neither binary, so on GitLab pipeline
#3241 (2026-08-25) thirteen of these guards failed that way in ``test:examlops`` and the
same thirteen in ``test:postgres`` — including the guard that stops private assistant
material reaching the *published* tree. The protection had never run in CI at all.

``require_binary`` turns that into one sentence naming the binary and what is consequently
unchecked. It **fails**; it deliberately does not skip. A skip is how this became invisible
in the first place — the guard has to be loud enough that someone installs the binary.
"""

from __future__ import annotations

import shutil

import pytest


def require_binary(name: str, guards: str) -> str:
    """Return the path to ``name``, or fail this test saying what is left unguarded.

    ``guards`` completes the sentence "this check is what ensures …", so the failure names
    the protection that is missing rather than only the tool.
    """
    found = shutil.which(name)
    if found is None:
        pytest.fail(
            f"cannot run this guard: {name!r} is not on PATH, so nothing here verified "
            f"that {guards}. This is an unguarded tree, not a passing one — install "
            f"{name!r} in the environment running the tests; a CI image is not required to "
            "have it (python:*-slim, for one, ships neither 'git' nor 'make').",
            pytrace=False,
        )
    return found
