# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for telemetrygen-k8s.

These tests deploy the charm to a real Juju/Kubernetes controller,
relate it to an OTLP receiver, and assert expected runtime behaviour.

Run with:
    tox -e integration

or directly:
    pytest tests/integration/ --model <model-name>

Requirements:
    - A bootstrapped Juju controller with a k8s cloud.
    - `tox -e integration` will pass JUJU_* env vars automatically if your
      Juju config is standard.

NOTE: These tests are written but not executed in CI without a live k8s
      controller.  They are intentionally skipped when the `jubilant` fixture
      is not available or the `CHARM_PATH` env var is unset.
"""

from __future__ import annotations

import os
import pathlib

import pytest

# jubilant is the Juju integration test library used in this project
try:
    import jubilant  # noqa: F401

    _JUBILANT_AVAILABLE = True
except ImportError:
    _JUBILANT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _JUBILANT_AVAILABLE,
    reason="jubilant not installed; skipping integration tests",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The charm under test.  Built by `charmcraft pack`; path supplied by tox.
CHARM_PATH = pathlib.Path(os.environ.get("CHARM_PATH", "telemetrygen-k8s.charm"))

# OTLP receiver charm from Charmhub.  opentelemetry-collector-k8s exposes the
# `receive-otlp` / `otlp` interface used by telemetrygen-k8s.
OTLP_RECEIVER_CHARM = "opentelemetry-collector-k8s"

# Relation names
TELEMETRYGEN_APP = "telemetrygen-k8s"
COLLECTOR_APP = "opentelemetry-collector-k8s"
OTLP_RELATION = "send-otlp"

# OCI image for the telemetrygen workload. `upstream-source` in charmcraft.yaml
# is only consumed by Charmhub-side publishing; local-pack deploys must pass
# the image as a `--resource` explicitly.
TELEMETRYGEN_IMAGE = (
    "ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.152.0"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def juju():
    """Provide a jubilant.Juju instance connected to a temporary model."""
    import jubilant

    with jubilant.temp_model() as _juju:
        yield _juju


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_charm() -> pathlib.Path:
    """Return path to the built charm file.

    If CHARM_PATH env var points to an existing file, use it directly.
    Otherwise try to locate a packed charm in the current directory.
    """
    if CHARM_PATH.exists():
        return CHARM_PATH

    # Fallback: look for any .charm file produced by charmcraft pack
    cwd = pathlib.Path.cwd()
    candidates = list(cwd.glob("*.charm"))
    if candidates:
        return candidates[0]

    pytest.skip(
        f"Charm file not found at {CHARM_PATH}. Build with `charmcraft pack` and set CHARM_PATH."
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_deploy_and_relate(juju):
    """Deploy telemetrygen-k8s and an OTLP receiver, relate them, assert both active.

    Steps:
    1. Build / locate the packed charm.
    2. Deploy the charm and a real OTLP receiver from Charmhub.
    3. Add the `send-otlp:send-otlp receive-otlp:receive-otlp` relation.
    4. Wait for both applications to reach Active status.
    5. Assert the workload (telemetrygen) Pebble service is running inside the
       telemetrygen-k8s unit's container.
    """
    import jubilant

    charm_path = _build_charm()

    # Deploy the charm under test (local pack). Resources must be passed
    # explicitly when deploying a local .charm file — `upstream-source` only
    # applies to Charmhub-published charms.
    juju.deploy(
        str(charm_path),
        app=TELEMETRYGEN_APP,
        config={"signals": "traces"},
        resources={"telemetrygen-image": TELEMETRYGEN_IMAGE},
        trust=True,
    )

    # Deploy the receiver from Charmhub.
    juju.deploy(
        OTLP_RECEIVER_CHARM,
        app=COLLECTOR_APP,
        channel="latest/stable",
        trust=True,
    )

    # Wait for both apps to be at least in a known state before relating.
    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, TELEMETRYGEN_APP)  # no relation yet
            or jubilant.all_waiting(status, TELEMETRYGEN_APP)
        ),
        timeout=300,
        delay=5,
    )

    # Relate via the OTLP interface.
    juju.relate(
        f"{TELEMETRYGEN_APP}:{OTLP_RELATION}",
        f"{COLLECTOR_APP}:receive-otlp",
    )

    # Both applications should reach Active.
    juju.wait(
        lambda status: (
            jubilant.all_active(status, TELEMETRYGEN_APP)
            and jubilant.all_active(status, COLLECTOR_APP)
        ),
        timeout=600,
        delay=10,
        error="Applications did not reach Active status after relating",
    )

    # Verify the Pebble service is running inside the workload container.
    result = juju.exec(
        f"{TELEMETRYGEN_APP}/0",
        command="pebble services",
        container="telemetrygen",
    )
    assert "active" in result.stdout.lower(), (
        f"Expected 'active' service in pebble output, got:\n{result.stdout}"
    )
    assert "telemetrygen-traces" in result.stdout, (
        f"Expected telemetrygen-traces service, got:\n{result.stdout}"
    )


def test_service_running_after_deploy(juju):
    """Assert the telemetrygen-traces Pebble service is active after full setup.

    This test assumes test_deploy_and_relate ran first (module-scope juju
    fixture keeps the model alive for the duration of the test module).
    """
    import jubilant

    # Both apps should still be active from the previous test.
    status = juju.status()
    assert jubilant.all_active(status, TELEMETRYGEN_APP), (
        f"{TELEMETRYGEN_APP} is not Active: {status}"
    )

    result = juju.exec(
        f"{TELEMETRYGEN_APP}/0",
        command="pebble services telemetrygen-traces",
        container="telemetrygen",
    )
    assert "active" in result.stdout.lower(), (
        f"telemetrygen-traces service is not active:\n{result.stdout}"
    )
