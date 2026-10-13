"""src.config reads its environment once, at first import. The modules here
import bridge code at collection time, before the billing/identity modules that
set BRIDGE_SERVICE_TOKEN at module level are collected; without this, those
modules then fail to import ("BRIDGE_SERVICE_TOKEN is required"). Same test
default as theirs (e.g. tests/jobs/test_capacity_veto.py)."""
import os

os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")
