"""Fixture: a proxy script that exists but dies before it can serve.

The preflight in lib/proxy.js rejects a *missing* script before spawning, so a
missing path can no longer exercise the "child exited before readiness" branch.
This file exists on disk and exits immediately, which is the only way to reach
that branch now.
"""
import sys

sys.exit(1)
