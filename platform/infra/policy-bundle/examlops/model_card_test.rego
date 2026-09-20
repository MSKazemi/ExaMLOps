package examlops.model_card_test

import rego.v1

import data.examlops.model_card

test_complete_card_allowed if model_card.allow with input as {"completeness": 0.9}

test_incomplete_card_denied if not model_card.allow with input as {"completeness": 0.5}

test_custom_floor_applies if not model_card.allow with input as {"completeness": 0.85, "floor": 0.9}

test_missing_completeness_denied if not model_card.allow with input as {}
