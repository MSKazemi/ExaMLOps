package examlops.budget_test

import rego.v1

import data.examlops.budget

test_within_budget_allowed if budget.allow with input as {"gpu_hours": 5, "budget": 10}

test_exactly_at_budget_allowed if budget.allow with input as {"gpu_hours": 10, "budget": 10}

test_over_budget_denied if not budget.allow with input as {"gpu_hours": 11, "budget": 10}

test_missing_inputs_denied if not budget.allow with input as {}
