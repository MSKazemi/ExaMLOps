"""Statistical analysis helpers for the platform (A/B testing, canary analysis).

Pure functions with no I/O so they are trivially unit-testable and reusable by the
A/B engine (#13), automated canary analysis (#11), and continuous evaluation (#14).
"""
