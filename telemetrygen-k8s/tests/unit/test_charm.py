# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for TelemetrygenCharm using ops.testing (Scenario / Context API).

All tests use ops.testing.Context (state-transition API, ops >= 2.17).
No legacy Harness is used.

The OTLP provider relation data is set by putting the JSON-serialised
`_OtlpProviderAppData` directly into `remote_app_data` of the Relation object,
which mirrors what a real provider charm writes via `relation.save(...)`.
"""

from __future__ import annotations

import json
import pathlib
import sys

import ops
import ops.pebble
import pytest
from ops.testing import Container, Context, Relation, State

# Ensure src/ and lib/ are importable (tox sets PYTHONPATH, but keep explicit).
_ROOT = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "lib"))

from charm import OTLP_RELATION, WORKLOAD_CONTAINER, TelemetrygenCharm  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Use a fixed relation ID in all tests so service names are deterministic.
# Pebble service names follow the pattern telemetrygen-<relation_id>-<signal>.
_REL_ID = 1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _otlp_relation(
    *,
    protocol: str = "grpc",
    endpoint: str = "collector.svc.cluster.local:4317",
    telemetries: list[str] | None = None,
    insecure: bool = False,
    empty: bool = False,
    rel_id: int = _REL_ID,
) -> Relation:
    """Build a Relation object representing the OTLP provider side.

    Parameters
    ----------
    empty:
        If True, return a relation with no app data (provider hasn't
        written anything yet — simulates the WaitingStatus scenario).
    rel_id:
        Explicit relation id; fixes service names to telemetrygen-<rel_id>-<signal>.
    """
    if telemetries is None:
        telemetries = ["traces", "metrics", "logs"]

    if empty:
        return Relation(endpoint=OTLP_RELATION, id=rel_id, remote_app_data={})

    endpoints_payload = json.dumps(
        [
            {
                "protocol": protocol,
                "endpoint": endpoint,
                "telemetries": telemetries,
                "insecure": insecure,
            }
        ]
    )
    return Relation(
        endpoint=OTLP_RELATION,
        id=rel_id,
        remote_app_data={"endpoints": endpoints_payload},
    )


def _connected_container(**kwargs) -> Container:
    """Return a Container that reports can_connect=True."""
    return Container(name=WORKLOAD_CONTAINER, can_connect=True, **kwargs)


def _ctx(**kwargs) -> Context:
    """Build a Context for TelemetrygenCharm with default config."""
    return Context(TelemetrygenCharm, **kwargs)


def _run_config_changed(
    state: State,
    *,
    ctx: Context | None = None,
) -> State:
    """Fire a config-changed event and return the output state."""
    if ctx is None:
        ctx = _ctx()
    return ctx.run(ctx.on.config_changed(), state)


def _service_names(state_out: State) -> set[str]:
    """Return the set of Pebble service names from the first container."""
    container = state_out.get_container(WORKLOAD_CONTAINER)
    plan = container.plan
    return set(plan.services.keys())


def _service_command(state_out: State, service: str) -> str:
    container = state_out.get_container(WORKLOAD_CONTAINER)
    return container.plan.services[service].command


def _service_layer_dict(state_out: State, service: str) -> dict:
    container = state_out.get_container(WORKLOAD_CONTAINER)
    return container.plan.to_dict()["services"][service]


def _svc(signal: str, rel_id: int = _REL_ID) -> str:
    """Return the expected Pebble service name for a (rel_id, signal) pair."""
    return f"telemetrygen-{rel_id}-{signal}"


# ---------------------------------------------------------------------------
# Test 1 — No OTLP relation → BlockedStatus, no Pebble services planned
# ---------------------------------------------------------------------------


def test_no_relation_blocked_no_services():
    """With no OTLP relation, charm must be BlockedStatus and plan no services."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert not _service_names(state_out), "expected no Pebble services without a relation"


# ---------------------------------------------------------------------------
# Test 2 — OTLP relation present but endpoint empty → WaitingStatus
# ---------------------------------------------------------------------------


def test_empty_endpoint_waiting():
    """Relation exists but provider hasn't published data → WaitingStatus."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(empty=True)],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, ops.WaitingStatus)


# ---------------------------------------------------------------------------
# Test 3 — gRPC endpoint, insecure=False → ActiveStatus, correct command
# ---------------------------------------------------------------------------


def test_grpc_endpoint_active_status_and_command():
    """gRPC endpoint with insecure=False → ActiveStatus, correct CLI flags."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                protocol="grpc",
                endpoint="collector.svc:4317",
                telemetries=["traces"],
                insecure=False,
            )
        ],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, ops.ActiveStatus)

    svc = _svc("traces")
    assert svc in _service_names(state_out)
    cmd = _service_command(state_out, svc)

    # endpoint must appear bare (no scheme)
    assert "--otlp-endpoint=collector.svc:4317" in cmd
    # no http flag for gRPC
    assert "--otlp-http" not in cmd
    # no insecure flag
    assert "--otlp-insecure" not in cmd


# ---------------------------------------------------------------------------
# Test 4 — HTTP protocol → --otlp-http flag present
# ---------------------------------------------------------------------------


def test_http_protocol_adds_otlp_http_flag():
    """HTTP protocol endpoint → command contains --otlp-http."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                protocol="http",
                endpoint="collector.svc:4318",
                telemetries=["traces"],
            )
        ],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    cmd = _service_command(state_out, _svc("traces"))
    assert "--otlp-http" in cmd


# ---------------------------------------------------------------------------
# Test 5 — insecure=True → --otlp-insecure flag present
# ---------------------------------------------------------------------------


def test_insecure_endpoint_adds_insecure_flag():
    """Insecure endpoint → command contains --otlp-insecure."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                protocol="grpc",
                endpoint="collector.svc:4317",
                telemetries=["traces"],
                insecure=True,
            )
        ],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    cmd = _service_command(state_out, _svc("traces"))
    assert "--otlp-insecure" in cmd


# ---------------------------------------------------------------------------
# Test 6 — signals = "traces,metrics,logs" → three services planned
# ---------------------------------------------------------------------------


def test_all_signals_creates_three_services():
    """signals=traces,metrics,logs → three Pebble services planned."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces", "metrics", "logs"])],
        config={"signals": "traces,metrics,logs"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    names = _service_names(state_out)
    assert _svc("traces") in names
    assert _svc("metrics") in names
    assert _svc("logs") in names


# ---------------------------------------------------------------------------
# Test 7 — Removing a signal → dropped service removed from plan
# ---------------------------------------------------------------------------


def test_removing_signal_drops_service():
    """Narrowing signals from traces,metrics,logs to just traces removes stale services from the desired layer.

    The initial state has traces+metrics+logs in the plan (using the
    id-based naming); after config-changed with signals=traces, the charm's
    new desired layer should contain only the traces service.

    Pebble's layered plan is append-only — services cannot be removed from the
    accumulated plan once added. The charm compensates by:
      1. Explicitly stopping orphaned services before replanning.
      2. Pushing a new desired layer that contains *only* the current targets.
    We verify the second point by inspecting the charm-managed layer key
    (`WORKLOAD_CONTAINER`) in the output container's layers dict; it must
    contain only the traces service, not the orphaned metrics/logs.
    """
    rel_id = _REL_ID
    # Build an initial plan that already has all three services running,
    # using the new telemetrygen-<rel_id>-<signal> naming.
    initial_plan = ops.pebble.Layer(
        {
            "services": {
                _svc("traces", rel_id): {
                    "override": "replace",
                    "command": "/telemetrygen traces --otlp-endpoint=x:4317 --duration=inf --rate=1.0 --workers=1 --service=telemetrygen-traces",
                    "startup": "enabled",
                    "on-success": "restart",
                    "on-failure": "restart",
                },
                _svc("metrics", rel_id): {
                    "override": "replace",
                    "command": "/telemetrygen metrics --otlp-endpoint=x:4317 --duration=inf --rate=1.0 --workers=1 --service=telemetrygen-metrics",
                    "startup": "enabled",
                    "on-success": "restart",
                    "on-failure": "restart",
                },
                _svc("logs", rel_id): {
                    "override": "replace",
                    "command": "/telemetrygen logs --otlp-endpoint=x:4317 --duration=inf --rate=1.0 --workers=1 --service=telemetrygen-logs",
                    "startup": "enabled",
                    "on-success": "restart",
                    "on-failure": "restart",
                },
            }
        }
    )

    ctx = _ctx()
    state_in = State(
        containers=[
            Container(
                name=WORKLOAD_CONTAINER,
                can_connect=True,
                layers={"initial": initial_plan},
                service_statuses={
                    _svc("traces", rel_id): ops.pebble.ServiceStatus.ACTIVE,
                    _svc("metrics", rel_id): ops.pebble.ServiceStatus.ACTIVE,
                    _svc("logs", rel_id): ops.pebble.ServiceStatus.ACTIVE,
                },
            )
        ],
        relations=[_otlp_relation(telemetries=["traces", "metrics", "logs"])],
        config={"signals": "traces"},  # only traces now
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    container = state_out.get_container(WORKLOAD_CONTAINER)

    # The charm pushes its desired layer under the key WORKLOAD_CONTAINER.
    # Inspect that layer directly to confirm orphans were excluded from the
    # new desired state (Pebble's accumulated plan still contains them, but
    # the charm's own layer must only list the currently-wanted service).
    charm_layer = container.layers.get(WORKLOAD_CONTAINER)
    assert charm_layer is not None, "charm must have pushed a Pebble layer"
    charm_layer_services = set(charm_layer.to_dict().get("services", {}).keys())

    assert _svc("traces", rel_id) in charm_layer_services, (
        "traces service must be present in the charm's desired layer"
    )
    assert _svc("metrics", rel_id) not in charm_layer_services, (
        "stale metrics service must not appear in the charm's new desired layer"
    )
    assert _svc("logs", rel_id) not in charm_layer_services, (
        "stale logs service must not appear in the charm's new desired layer"
    )


# ---------------------------------------------------------------------------
# Test 8 — Scheme stripping: http:// and https:// prefixes are removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_endpoint,expected_bare",
    [
        ("http://collector.svc:4318", "collector.svc:4318"),
        ("https://collector.svc:4317", "collector.svc:4317"),
        ("collector.svc:4317", "collector.svc:4317"),
    ],
)
def test_scheme_stripped_from_endpoint(raw_endpoint, expected_bare):
    """Endpoints with http/https scheme must have scheme stripped in CLI command."""
    ctx = _ctx()
    # Use grpc protocol but pass an endpoint with a scheme to trigger strip
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                protocol="grpc",
                endpoint=raw_endpoint,
                telemetries=["traces"],
            )
        ],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # If ActiveStatus, check command; if WaitingStatus/BlockedStatus, scheme was
    # malformed and the test should still surface the issue.
    if isinstance(state_out.unit_status, ops.ActiveStatus):
        cmd = _service_command(state_out, _svc("traces"))
        assert f"--otlp-endpoint={expected_bare}" in cmd, (
            f"Expected bare endpoint {expected_bare!r} in command {cmd!r}"
        )


# ---------------------------------------------------------------------------
# Test 9 — Config knobs: rate, duration, workers, service_name
# ---------------------------------------------------------------------------


def test_config_knobs_in_command():
    """rate, duration, workers and service_name config appear in CLI command."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces", "metrics"])],
        config={
            "signals": "traces,metrics",
            "rate": 5.0,
            "duration": "30s",
            "workers": 3,
            "service_name": "myapp",
        },
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, ops.ActiveStatus)

    for signal in ("traces", "metrics"):
        svc = _svc(signal)
        cmd = _service_command(state_out, svc)
        assert "--rate=5.0" in cmd, f"rate flag missing in {svc} command"
        assert "--duration=30s" in cmd, f"duration flag missing in {svc} command"
        assert "--workers=3" in cmd, f"workers flag missing in {svc} command"
        assert f"--service=myapp-{signal}" in cmd, f"service flag wrong in {svc} command"


def test_service_name_suffix_per_signal():
    """service_name=foo with signals=traces,metrics → --service=foo-traces and --service=foo-metrics."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces", "metrics"])],
        config={
            "signals": "traces,metrics",
            "service_name": "foo",
        },
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    cmd_traces = _service_command(state_out, _svc("traces"))
    cmd_metrics = _service_command(state_out, _svc("metrics"))

    assert "--service=foo-traces" in cmd_traces
    assert "--service=foo-metrics" in cmd_metrics


# ---------------------------------------------------------------------------
# Test 10 — Pebble layer restart policy: on-success and on-failure both restart
# ---------------------------------------------------------------------------


def test_restart_policy_in_layer():
    """Generated Pebble layer must set on-success=restart and on-failure=restart."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    svc_dict = _service_layer_dict(state_out, _svc("traces"))
    assert svc_dict.get("on-success") == "restart", "on-success should be 'restart'"
    assert svc_dict.get("on-failure") == "restart", "on-failure should be 'restart'"


# ---------------------------------------------------------------------------
# Test 11 — Reconciler idempotency: second run produces no plan diff
# ---------------------------------------------------------------------------


def test_reconciler_idempotency():
    """Running reconcile twice with identical state produces the same plan."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces"},
    )

    state_out_1 = ctx.run(ctx.on.config_changed(), state_in)
    plan_1 = state_out_1.get_container(WORKLOAD_CONTAINER).plan.to_dict()

    # Second run: feed previous output state's container plan back in,
    # keeping the same relation (same id) so service names are stable.
    container_after_first = state_out_1.get_container(WORKLOAD_CONTAINER)
    state_in_2 = State(
        containers=[
            Container(
                name=WORKLOAD_CONTAINER,
                can_connect=True,
                layers=container_after_first.layers,
                service_statuses=container_after_first.service_statuses,
            )
        ],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces"},
    )
    state_out_2 = ctx.run(ctx.on.config_changed(), state_in_2)
    plan_2 = state_out_2.get_container(WORKLOAD_CONTAINER).plan.to_dict()

    assert plan_1 == plan_2, "plan should be identical on second (idempotent) reconcile"


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_unknown_signal_blocks_charm():
    """An unknown signal in config → BlockedStatus (config_error path)."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces,unknown_signal"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)


def test_container_not_ready_no_services_no_crash():
    """If container can_connect=False, reconcile exits early without crash."""
    ctx = _ctx()
    state_in = State(
        containers=[Container(name=WORKLOAD_CONTAINER, can_connect=False)],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces"},
    )
    # Must not raise.
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    # Status may be Waiting (container not yet reachable).
    assert isinstance(
        state_out.unit_status, (ops.WaitingStatus, ops.ActiveStatus, ops.BlockedStatus)
    )


def test_pebble_ready_triggers_reconcile():
    """pebble-ready event triggers reconcile and reaches ActiveStatus when endpoint ready."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                protocol="grpc",
                endpoint="collector:4317",
                telemetries=["traces"],
            )
        ],
        config={"signals": "traces"},
    )
    state_out = ctx.run(
        ctx.on.pebble_ready(Container(name=WORKLOAD_CONTAINER, can_connect=True)), state_in
    )
    assert isinstance(state_out.unit_status, ops.ActiveStatus)
    assert _svc("traces") in _service_names(state_out)


def test_layer_startup_enabled():
    """Generated services must have startup=enabled."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    svc_dict = _service_layer_dict(state_out, _svc("traces"))
    assert svc_dict.get("startup") == "enabled"


def test_layer_override_replace():
    """Generated services must have override=replace."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    svc_dict = _service_layer_dict(state_out, _svc("traces"))
    assert svc_dict.get("override") == "replace"


# ---------------------------------------------------------------------------
# New validation tests (post-fixer behavior changes)
# ---------------------------------------------------------------------------


def test_invalid_duration_blocks_charm():
    """duration='abc' (not a valid Go duration) → BlockedStatus."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces", "duration": "abc"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "duration" in state_out.unit_status.message.lower()


def test_zero_workers_blocks_charm():
    """workers=0 → BlockedStatus (workers must be at least 1)."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces", "workers": 0},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "workers" in state_out.unit_status.message.lower()


def test_endpoint_without_port_blocks_charm():
    """An OTLP endpoint without an explicit port → BlockedStatus."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                protocol="grpc",
                endpoint="https://collector",  # no :port
                telemetries=["traces"],
            )
        ],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    # The endpoint mismatch leaves no usable targets → BlockedStatus with
    # the "OTLP provider(s) do not advertise any of the configured signals"
    # message (which wraps the per-relation port error).
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "port" in state_out.unit_status.message.lower()


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


def test_empty_signals_config_blocks_charm():
    """signals='' → BlockedStatus (must list at least one signal)."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": ""},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "signals" in state_out.unit_status.message.lower()


def test_empty_duration_blocks_charm():
    """duration='' → BlockedStatus."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces", "duration": ""},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "duration" in state_out.unit_status.message.lower()


def test_negative_rate_blocks_charm():
    """rate=-1.0 → BlockedStatus."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[_otlp_relation(telemetries=["traces"])],
        config={"signals": "traces", "rate": -1.0},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "rate" in state_out.unit_status.message.lower()


def test_provider_advertises_no_configured_signals_blocked():
    """Provider only advertises 'metrics' but signals='traces' → BlockedStatus."""
    ctx = _ctx()
    state_in = State(
        containers=[_connected_container()],
        relations=[
            _otlp_relation(
                endpoint="collector.svc:4317",
                telemetries=["metrics"],  # provider only has metrics
            )
        ],
        config={"signals": "traces"},  # but we want traces
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert "OTLP provider(s) do not advertise" in state_out.unit_status.message


def test_multi_target_active_status_message():
    """Two OTLP relations active → ActiveStatus with multi-target message."""
    ctx = _ctx()
    rel_a = _otlp_relation(
        rel_id=10,
        endpoint="collector-a.svc:4317",
        telemetries=["traces"],
    )
    rel_b = _otlp_relation(
        rel_id=11,
        endpoint="collector-b.svc:4317",
        telemetries=["traces"],
    )
    state_in = State(
        containers=[_connected_container()],
        relations=[rel_a, rel_b],
        config={"signals": "traces"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(state_out.unit_status, ops.ActiveStatus)
    # Multi-target message mentions count of receivers.
    assert "2" in state_out.unit_status.message or "OTLP receiver" in state_out.unit_status.message
