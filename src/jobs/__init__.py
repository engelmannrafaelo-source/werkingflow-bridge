"""Generic async-job system for the Bridge.

See docker/migrations/031_ai_jobs.sql and bridge-async-jobs-spec.md.

Layout:
  store.py     — Postgres-backed durable job store (no app/main.py deps)
  registry.py  — kind → executor registry + the generic background runner
  routes.py    — POST /v1/jobs + GET /v1/jobs/{id} (feature-flagged, additive)

main.py wires it: includes the router, registers executors at startup, and runs
the watchdog/cleanup task. Inert unless BRIDGE_GENERIC_JOBS_ENABLED=true.
"""
