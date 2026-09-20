package examlops.budget

import rego.v1

# A GPU request over budget is denied (ADR 0029 decision 2, FinOps/E3).
default allow := false

allow if input.gpu_hours <= input.budget
