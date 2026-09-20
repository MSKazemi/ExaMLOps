package examlops.model_card

import rego.v1

# Model-card completeness must meet the floor before promotion (ADR 0029 decision 2, A6).
default allow := false

floor := object.get(input, "floor", 0.8)

allow if input.completeness >= floor
