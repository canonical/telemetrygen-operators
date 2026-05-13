# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Happy-path scenario: telemetry actually reaches a real OTLP receiver."""

from __future__ import annotations

import jubilant
from pytest_bdd import given, scenarios, then, when

from . import helpers

# pytest-bdd needs the .feature file path resolved relative to this module.
scenarios("features/happy_path.feature")


# --- Given ------------------------------------------------------------------


@given("the telemetrygen-k8s charm is deployed and blocked on the missing OTLP relation")
@given("an opentelemetry-collector-k8s receiver is deployed with debug-export enabled for traces")
def _baseline(deployment: jubilant.Juju) -> jubilant.Juju:
    """Both Givens are satisfied by the session-scoped `deployment` fixture.

    Mapping multiple Given clauses onto the same fixture is intentional: the
    feature file reads naturally to a human, while the test code keeps a
    single source of truth for the deployed state.
    """
    return deployment


# --- When -------------------------------------------------------------------


@when("I relate telemetrygen-k8s to the receiver on send-otlp / receive-otlp")
def _relate(deployment: jubilant.Juju) -> None:
    """No-op: the deployment fixture already relates the two apps.

    The Gherkin step is here to make the scenario readable; the action it
    describes has already happened at fixture-setup time.
    """
    # Sanity check: status confirms the relation is in place.
    status = deployment.status()
    rels = status.apps[helpers.TELEMETRYGEN_APP].relations
    assert rels.get(helpers.TELEMETRYGEN_OTLP_ENDPOINT), (
        f"expected send-otlp relation to exist on telemetrygen-k8s; found relations: {dict(rels)}"
    )


# --- Then -------------------------------------------------------------------


@then("telemetrygen-k8s reaches Active status")
def _tg_active(deployment: jubilant.Juju) -> None:
    status = deployment.status()
    assert jubilant.all_active(status, helpers.TELEMETRYGEN_APP), (
        f"expected {helpers.TELEMETRYGEN_APP} active; got "
        f"{status.apps[helpers.TELEMETRYGEN_APP].app_status}"
    )


@then("the traces pebble service is running in the workload container")
def _telemetrygen_traces_running(deployment: jubilant.Juju) -> None:
    relation_id = helpers.collector_relation_id(deployment)
    expected = f"telemetrygen-{relation_id}-traces"
    running = helpers.running_services(
        deployment,
        unit=f"{helpers.TELEMETRYGEN_APP}/0",
        container=helpers.TELEMETRYGEN_CONTAINER,
    )
    assert expected in running, (
        f"expected pebble service {expected!r} to be running; running services: {sorted(running)}"
    )


@then("the receiver's workload logs contain at least one received trace span")
def _spans_received(deployment: jubilant.Juju) -> None:
    # tenacity handles the poll-and-retry; if no spans show up after ~2.5min
    # we re-raise the underlying AssertionError so pytest surfaces it.
    helpers.assert_traces_received_by_collector(deployment)
