# Example data-center policy for ExaMLOps, served by Open Policy Agent (ADR 0120).
#
# ExaMLOps calls   POST <opa>/v1/data/examlops/decision   with {"input": <AuthZEN request>}:
#
#   input.subject.id                      "<provider>:<sub>"
#   input.subject.properties.{tenant, role, groups, username, email, acr, scopes, issuer}
#   input.action.name                     "api.read" | "api.write" | a capability ("model.promote", …)
#   input.resource.{type, id, properties}  e.g. {"type": "control_plane", "id": "/retrain",
#                                               "properties": {"method": "POST", "tenant": "lab"}}
#   input.context.time                    ISO-8601 UTC
#
# It must answer {"result": {"allow": <bool>, "reason": "<text>"}} (or a bare boolean). An undefined
# result is a deny. This runs *after* the platform's own checks (tenant isolation, role ladder), so
# a center only ever narrows what ExaMLOps allows — "authorization.mode: both" (deny-overrides).
package examlops

import rego.v1

# One ordered chain, so exactly one answer is produced for any input (two complete rules with
# different values would be an OPA evaluation error — which ExaMLOps treats as a deny anyway).
decision := {"allow": true, "reason": "reads are allowed"} if {
	input.action.name == "api.read"
} else := {"allow": false, "reason": "change freeze: no writes on weekends"} if {
	frozen
} else := {"allow": false, "reason": "promoting a model needs an MFA login at the center"} if {
	promotion_needs_mfa
} else := {"allow": true, "reason": "write allowed by the center's policy"} if {
	is_string(input.action.name) # a malformed request never falls through to a permit
}

default decision := {"allow": false, "reason": "no rule of the center's policy permits this"}

# Example site rule: nothing changes on Saturday or Sunday (UTC).
frozen if {
	day := time.weekday(time.parse_rfc3339_ns(input.context.time))
	day in {"Saturday", "Sunday"}
}

# Example site rule: model promotion requires the REFEDS MFA profile.
promotion_needs_mfa if {
	input.action.name == "model.promote"
	input.subject.properties.acr != "https://refeds.org/profile/mfa"
}
