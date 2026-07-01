"""FinOps + Green-AI accounting (#20).

Pure cost/energy/carbon estimation helpers (no I/O) plus the ``exa finops`` CLI.
Reuses the existing ``model_costs`` (GPU-hours) and ``namespace_models`` (project
membership) tables so a "project" is a namespace, and budgets are enforced against
real recorded consumption.
"""
