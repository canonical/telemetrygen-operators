# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Reconfiguration: switching signals while related reshapes pebble services."""

from __future__ import annotations

import jubilant
from pytest_bdd import given, scenarios, then

from . import helpers  # noqa: F401 — keeps the module imported for step resolution

scenarios("features/signal_switch.feature")


# --- Given ------------------------------------------------------------------


@given("only the traces pebble service is running")
def _only_traces_running(reset_to_baseline: jubilant.Juju) -> None:
    """For the current relation, exactly the traces service should be active.

    We deliberately filter by the current relation id rather than asserting
    "no other telemetrygen-* services exist anywhere". Earlier tests that
    re-relate the charm (e.g. relation_departed) leave orphaned `telemetrygen-
    <old-rel-id>-*` entries in the Pebble plan from the previous relation
    incarnation, which Pebble's replan can re-spawn — that's a charm-level
    concern about plan churn across relation-departed-then-rejoined, not a
    regression in the signals-switch scenario we're exercising here.
    """
    relation_id = helpers.collector_relation_id(reset_to_baseline)
    prefix = f"telemetrygen-{relation_id}-"
    expected = {f"{prefix}traces"}
    current_rel_running = {
        s
        for s in helpers.running_services(
            reset_to_baseline,
            unit=f"{helpers.TELEMETRYGEN_APP}/0",
            container=helpers.TELEMETRYGEN_CONTAINER,
        )
        if s.startswith(prefix)
    }
    assert current_rel_running == expected, (
        f"expected exactly {expected} to be running for current relation; "
        f"got {current_rel_running}"
    )


# --- Then -------------------------------------------------------------------


@then("traces, metrics, and logs pebble services are all running")
def _all_three_signals_running(reset_to_baseline: jubilant.Juju) -> None:
    relation_id = helpers.collector_relation_id(reset_to_baseline)
    expected = helpers.predicted_service_names(relation_id, ("traces", "metrics", "logs"))

    import tenacity

    prefix = f"telemetrygen-{relation_id}-"

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(30),
        wait=tenacity.wait_fixed(5),
        reraise=True,
    )
    def _check() -> None:
        running = {
            s
            for s in helpers.running_services(
                reset_to_baseline,
                unit=f"{helpers.TELEMETRYGEN_APP}/0",
                container=helpers.TELEMETRYGEN_CONTAINER,
            )
            if s.startswith(prefix)
        }
        missing = expected - running
        assert not missing, (
            f"expected {sorted(expected)} to all be running; "
            f"missing: {sorted(missing)}; running: {sorted(running)}"
        )

    _check()
