package examlops.supply_chain

import rego.v1

# An unsigned artifact is never allowed to deploy (ADR 0029 decision 2, D3).
default allow := false

allow if input.signed == true
