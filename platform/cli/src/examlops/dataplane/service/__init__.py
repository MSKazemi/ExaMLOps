"""Dataplane service: the pull scheduler and its FastAPI app (ADR 0130 §9).

Docstring-only on purpose: importing this package must not pull in FastAPI. Only
``examlops.dataplane.service.app`` (added later) imports it; ``scheduler.py`` here is pure stdlib.
"""
