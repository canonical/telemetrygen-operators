# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Failure mode: invalid config drives the unit to Blocked, then recovers."""

from __future__ import annotations

from pytest_bdd import scenarios

from . import helpers  # noqa: F401 — keeps the module imported for step resolution

scenarios("features/invalid_config.feature")
