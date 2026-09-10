"""The committed api-contract.json is the control plane's public API contract.

The production incidents this guard exists for were contract drift discovered at runtime (a
deployed image missing a package, a dashboard button 503ing): a consumer learned about an
interface change only by breaking. Committing the reduced contract turns every route,
parameter, and response-code change into a reviewable diff in the same commit that made it —
and an *accidental* change into a red test. The reduction (see ``api_contract.py``) is what
makes the guard portable: a raw ``app.openapi()`` snapshot differs between fastapi versions
with no interface change at all.

After an intentional API change: ``make openapi-export``, review the diff, commit it.
"""

import json

import api_contract
import app as app_mod


def test_api_matches_committed_contract():
    committed = json.loads(api_contract.CONTRACT_PATH.read_text())
    live = api_contract.reduce_spec(app_mod.app.openapi())

    # Route-set drift first: a one-line assertion beats a whole-contract dict diff when the
    # change is an added or removed endpoint.
    assert sorted(live["paths"]) == sorted(committed["paths"]), (
        "the control plane's route set no longer matches the committed api-contract.json — "
        "if intentional, run `make openapi-export` and commit the diff"
    )
    assert live == committed, (
        "the control plane's API (methods/parameters/response codes) drifted from the "
        "committed api-contract.json — if intentional, run `make openapi-export` and commit "
        "the diff; if not, an interface changed by accident"
    )
