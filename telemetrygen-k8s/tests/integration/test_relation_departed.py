# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Failure mode: dropping the OTLP relation returns the unit to Blocked."""

from __future__ import annotations

import jubilant
from pytest_bdd import scenarios, when

from . import helpers  # noqa: F401 — keeps the module imported for step resolution

scenarios("features/relation_departed.feature")


# --- When -------------------------------------------------------------------


@when("I remove the relation between telemetrygen-k8s and the OTLP receiver")
def _remove_relation(reset_to_baseline: jubilant.Juju) -> None:
    reset_to_baseline.remove_relation(
        f"{helpers.TELEMETRYGEN_APP}:{helpers.TELEMETRYGEN_OTLP_ENDPOINT}",
        f"{helpers.COLLECTOR_APP}:{helpers.COLLECTOR_OTLP_ENDPOINT}",
    )


@when("I re-add the relation between telemetrygen-k8s and the OTLP receiver")
def _readd_relation(reset_to_baseline: jubilant.Juju) -> None:
    reset_to_baseline.integrate(
        f"{helpers.TELEMETRYGEN_APP}:{helpers.TELEMETRYGEN_OTLP_ENDPOINT}",
        f"{helpers.COLLECTOR_APP}:{helpers.COLLECTOR_OTLP_ENDPOINT}",
    )
