package examlops.supply_chain_test

import rego.v1

import data.examlops.supply_chain

test_signed_artifact_allowed if supply_chain.allow with input as {"signed": true}

test_unsigned_artifact_denied if not supply_chain.allow with input as {"signed": false}

test_missing_signed_field_denied if not supply_chain.allow with input as {}
