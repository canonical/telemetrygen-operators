# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for telemetrygen-k8s integration tests.

Layout follows the persona's "one feature per file" rule:
* `features/<scenario>.feature` — Gherkin scenarios written as user stories
* `test_<scenario>.py` — pytest-bdd step bindings, one file per feature

The deployment fixture is session-scoped: each test_*.py shares a single
`telemetrygen-k8s + opentelemetry-collector-k8s` deployment in a per-session
temporary Juju model. Per-test state mutations (signals/duration tweaks,
relation add/remove) are reset by a function-scoped fixture so that each
scenario starts from a known-good Active state.

We deliberately do NOT skip when `jubilant` or the charm artifact is missing
— tests that quietly skip never break. Either fail with an explicit error,
or run.
"""

from __future__ import annotations

import logging
import os
import subprocess

import jubilant
import pytest

from . import helpers

logger = logging.getLogger(__name__)


# ---- pytest-bdd shared steps -------------------------------------------------
# pytest-bdd auto-discovers steps from conftest files.  Defining shared step
# patterns here (rather than in a separate module) avoids import-order issues.
from pytest_bdd import given, parsers, then, when  # noqa: E402


@given("the telemetrygen-k8s charm is deployed and related to the OTLP receiver")
@given("telemetrygen-k8s is Active")
def _baseline_active(reset_to_baseline: jubilant.Juju) -> jubilant.Juju:
    """Given used by invalid_config, relation_departed, signal_switch."""
    return reset_to_baseline


@when(parsers.parse('I set the telemetrygen-k8s "{option}" config to "{value}"'))
def _set_config(reset_to_baseline: jubilant.Juju, option: str, value: str) -> None:
    reset_to_baseline.config(helpers.TELEMETRYGEN_APP, values={option: value})


@when(parsers.parse('I reset the telemetrygen-k8s "{option}" config back to "{value}"'))
def _reset_config(reset_to_baseline: jubilant.Juju, option: str, value: str) -> None:
    reset_to_baseline.config(helpers.TELEMETRYGEN_APP, values={option: value})


@then(
    parsers.parse('telemetrygen-k8s reaches Blocked status with a message mentioning "{needle}"')
)
def _blocked_with_message(reset_to_baseline: jubilant.Juju, needle: str) -> None:
    def is_blocked_with_message(status: jubilant.statustypes.Status) -> bool:
        if not jubilant.all_blocked(status, helpers.TELEMETRYGEN_APP):
            return False
        unit = next(iter(status.apps[helpers.TELEMETRYGEN_APP].units.values()))
        return needle in unit.workload_status.message

    reset_to_baseline.wait(
        is_blocked_with_message,
        error=jubilant.any_error,
        timeout=5 * 60,
        delay=5,
    )


@then("telemetrygen-k8s returns to Active status")
def _back_to_active(reset_to_baseline: jubilant.Juju) -> None:
    reset_to_baseline.wait(
        lambda s: jubilant.all_active(s, helpers.TELEMETRYGEN_APP),
        error=jubilant.any_error,
        timeout=10 * 60,
        delay=5,
    )


# Per the task brief, any pre-existing telemetrygen-test model is destroyed
# before the session starts. `jubilant.temp_model()` uses random names so we
# also clean up anything matching that legacy-name pattern in case a previous
# run was interrupted.
_LEGACY_MODEL_NAMES = ("telemetrygen-test",)


def _destroy_legacy_models() -> None:
    """Best-effort destroy of any pre-existing test models from prior runs."""
    for name in _LEGACY_MODEL_NAMES:
        # `juju destroy-model` exits non-zero if the model doesn't exist,
        # which is the common case — we don't want that to abort the session.
        subprocess.run(
            [
                "juju",
                "destroy-model",
                name,
                "--no-prompt",
                "--destroy-storage",
                "--force",
                "--no-wait",
            ],
            check=False,
            capture_output=True,
        )


def pytest_configure(config: pytest.Config) -> None:  # noqa: D103
    _destroy_legacy_models()


@pytest.fixture(scope="session")
def juju():
    """Provide a jubilant.Juju instance against a fresh temporary model.

    The model is destroyed at session exit. Tests share the deployed charms,
    so each test file should reset state via the `reset_to_baseline` fixture.
    """
    with jubilant.temp_model() as j:
        # Longer wait_timeout: image pulls + first-deploy reconcile can take
        # a while on a cold microk8s.
        j.wait_timeout = 20 * 60
        logger.info("Created temp model %s", j.model)
        yield j


@pytest.fixture(scope="session")
def deployment(juju: jubilant.Juju) -> jubilant.Juju:
    """Deploy telemetrygen-k8s + opentelemetry-collector-k8s, wait for Active.

    Returns the same `juju` instance — the fixture's job is just to gate
    on a fully-converged deployment.
    """
    charm_path = helpers.charm_path()
    logger.info("Deploying telemetrygen-k8s from %s", charm_path)

    juju.deploy(
        str(charm_path),
        app=helpers.TELEMETRYGEN_APP,
        resources={"telemetrygen-image": helpers.TELEMETRYGEN_IMAGE},
        trust=True,
        # Defaults from charmcraft.yaml; spelled out for clarity since each
        # scenario relies on them as the baseline state.
        config={
            "signals": "traces",
            "duration": "inf",
            "rate": 1.0,
            "workers": 1,
        },
    )

    juju.deploy(
        helpers.COLLECTOR_CHARM,
        app=helpers.COLLECTOR_APP,
        channel=helpers.COLLECTOR_CHANNEL,
        trust=True,
        # debug exporter dumps every received batch to stdout, which is how
        # the happy-path test verifies telemetry actually arrived.
        config={
            "debug_exporter_for_traces": True,
            "debug_exporter_for_metrics": True,
            "debug_exporter_for_logs": True,
        },
    )

    # NOTE: opentelemetry-collector-k8s deliberately goes BlockedStatus when
    # `receive-otlp` is hooked up but no `send-*` (loki/remote-write/...) is
    # related to forward the data downstream. The OTLP receiver and the
    # configured debug exporter still run — data arrives and is dumped to the
    # workload's stdout (see assert_traces_received_by_collector in helpers).
    # We therefore only gate on telemetrygen-k8s reaching Active; the
    # collector's "no-export-path" Blocked is expected and tolerated.

    # Before relating, telemetrygen should be Blocked on the missing relation.
    juju.wait(
        lambda status: jubilant.all_blocked(status, helpers.TELEMETRYGEN_APP),
        error=jubilant.any_error,
        timeout=20 * 60,
        delay=5,
    )

    juju.integrate(
        f"{helpers.TELEMETRYGEN_APP}:{helpers.TELEMETRYGEN_OTLP_ENDPOINT}",
        f"{helpers.COLLECTOR_APP}:{helpers.COLLECTOR_OTLP_ENDPOINT}",
    )

    juju.wait(
        lambda status: jubilant.all_active(status, helpers.TELEMETRYGEN_APP),
        error=jubilant.any_error,
        timeout=20 * 60,
        delay=5,
    )

    return juju


@pytest.fixture
def reset_to_baseline(deployment: jubilant.Juju):
    """Function-scoped state reset.

    Restores `signals`, `duration`, and the send-otlp relation so each
    scenario starts from the same baseline regardless of what the previous
    scenario did. Skips redundant churn (e.g. doesn't reset config that is
    already at the default).
    """
    juju = deployment

    yield juju

    # Reset config to defaults.
    juju.config(
        helpers.TELEMETRYGEN_APP,
        values={
            "signals": "traces",
            "duration": "inf",
        },
    )

    # Re-add the relation if a previous test removed it.
    status = juju.status()
    rels = status.apps[helpers.TELEMETRYGEN_APP].relations
    if (
        helpers.TELEMETRYGEN_OTLP_ENDPOINT not in rels
        or not rels[helpers.TELEMETRYGEN_OTLP_ENDPOINT]
    ):
        juju.integrate(
            f"{helpers.TELEMETRYGEN_APP}:{helpers.TELEMETRYGEN_OTLP_ENDPOINT}",
            f"{helpers.COLLECTOR_APP}:{helpers.COLLECTOR_OTLP_ENDPOINT}",
        )

    juju.wait(
        lambda status: jubilant.all_active(status, helpers.TELEMETRYGEN_APP),
        error=jubilant.any_error,
        timeout=10 * 60,
        delay=5,
    )


# Capture juju debug-log on failure so the operator output is on the CI
# console without grepping through the model post-mortem.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):  # noqa: D103
    outcome = yield
    rep = outcome.get_result()
    if rep.failed and rep.when in ("setup", "call"):
        juju = item.funcargs.get("deployment") or item.funcargs.get("juju")
        if isinstance(juju, jubilant.Juju):
            try:
                logger.error(
                    "juju debug-log (last 500 lines):\n%s",
                    juju.debug_log(limit=500),
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to fetch juju debug-log")


# Disable verbose jubilant.wait status spam during long waits — we still
# want INFO-level logs, just not the per-poll status dumps.
logging.getLogger("jubilant.wait").setLevel(os.environ.get("JUBILANT_WAIT_LOG", "WARNING"))
