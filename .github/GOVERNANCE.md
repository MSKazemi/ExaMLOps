# Governance

ExaMLOps is young, and its governance is deliberately lightweight. This document says how
decisions are made today and how that changes as the community grows.

## Roles

| Role | Who | Can |
|---|---|---|
| **User** | anyone running ExaMLOps | report issues, ask questions, share field reports |
| **Contributor** | anyone with a merged contribution — code, docs, tests, reviews, triage, design | everything above; is credited in the CHANGELOG and release notes |
| **Reviewer** | a contributor invited after sustained, high-quality contributions in an area | review and approve pull requests in that area; label and triage issues |
| **Maintainer** | a reviewer invited by the existing maintainers | merge, release, set the roadmap, enforce the Code of Conduct |

Current maintainer: **Mohsen Seyedkazemi Ardebili** ([@MSKazemi](https://github.com/MSKazemi)),
project lead.

### Becoming a reviewer or maintainer

There is no fixed quota. As a rough guide, a contributor who has landed several non-trivial
pull requests in one area, reviews others' work constructively, and follows the Code of
Conduct will be invited to become a reviewer for that area. Reviewers who have been active for
a few months and understand the architecture boundaries are invited to become maintainers.
You are welcome to ask what would get you there.

## How decisions are made

- **Day-to-day changes** are decided in the pull request: CI green, tests and docs present,
  one maintainer approval.
- **Significant design changes** — a new subsystem, a public API or CLI break, a new
  runtime dependency, a change to a security boundary — start as an issue labelled
  `proposal` describing the problem, the options and the recommendation. Discussion stays
  open for at least 7 days before a decision.
- **Accepted designs are recorded as Architecture Decision Records** by the maintainers,
  so the reason for a choice outlives the thread it was made in.
- The project lead decides when consensus cannot be reached, and explains the decision in
  writing on the issue.

As the project gains more maintainers, this model will move to maintainer consensus with
lazy consensus for routine changes. Changes to this document are themselves proposals.

## Code of Conduct

Everyone participating in the project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
Maintainers enforce it.
