#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for the OpenTelemetry `telemetrygen` synthetic OTLP client.

The charm wraps the upstream `telemetrygen` CLI and runs it under Pebble
inside a sidecar workload container. It receives OTLP destinations from one
or more related charms via the modern unified `otlp` relation interface
(provided by the `charmlibs.interfaces.otlp` PyPI library) and converts each
endpoint into the appropriate `telemetrygen` CLI invocation.

The charm follows the Canonical Observability convention of a single
`_reconcile` method that converges the workload toward the desired state on
every observed event.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import ops
from charmlibs.interfaces.otlp import OtlpEndpoint, OtlpRequirer
from cosl.reconciler import observe_events, reconcilable_events_k8s

logger = logging.getLogger(__name__)


# --- Constants ----------------------------------------------------------------

WORKLOAD_CONTAINER = "telemetrygen"
"""Name of the Pebble container that hosts the telemetrygen binary."""

OTLP_RELATION = "send-otlp"
"""Charm-side relation name for the OTLP requirer endpoint."""

VALID_SIGNALS: tuple[str, ...] = ("traces", "metrics", "logs")
"""Signal names accepted both by `telemetrygen` subcommands and by config."""

SERVICE_PREFIX = "telemetrygen-"
"""All Pebble services managed by this charm start with this prefix."""

# `telemetrygen` is shipped at /telemetrygen inside the upstream image.
# The image's entrypoint is the binary itself; we invoke it explicitly here
# so the Pebble layer is independent of any future image entrypoint change.
TELEMETRYGEN_BINARY = "/telemetrygen"

# telemetrygen's `--duration` flag accepts either the literal `inf` or a Go
# `time.Duration` string. Go durations are one or more `<number><unit>`
# pairs, where unit is one of ns/us/µs/ms/s/m/h. We accept the common
# integer/decimal forms here; if the user needs an exotic value they can
# express it as e.g. `3600s` instead of `1h`.
_DURATION_RE = re.compile(r"^(?:inf|(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+)$")


# --- Helpers ------------------------------------------------------------------


@dataclass(frozen=True)
class _RelationTarget:
    """A single `(relation, endpoint, signals)` reconcile target.

    `signals` here is the *intersection* of the user-configured signals and
    the telemetry types this particular provider advertises on its endpoint.
    It is always non-empty (callers filter out empty intersections so the
    charm can surface a clean BlockedStatus instead).
    """

    relation_id: int
    endpoint: OtlpEndpoint
    signals: Tuple[str, ...]


@dataclass(frozen=True)
class _DesiredState:
    """Snapshot of everything `_reconcile` needs to converge to.

    Computing this once per reconcile means the rest of the reconcile flow is
    pure (no further relation/config reads), which makes the charm trivial to
    reason about and to unit-test.
    """

    configured_signals: Tuple[str, ...]
    rate: float
    duration: str
    workers: int
    service_name: str
    targets: Tuple[_RelationTarget, ...]
    config_error: Optional[str]
    """If non-None, a human-readable explanation of an invalid charm config."""
    signal_mismatches: Tuple[str, ...]
    """Per-relation messages where the provider advertised none of the
    configured signals. Surfaced via BlockedStatus when there are no usable
    targets, and logged as warnings when there are partial mismatches."""


def _parse_signals(raw: str) -> Tuple[Tuple[str, ...], Optional[str]]:
    """Parse and validate the `signals` config option.

    Returns:
        A tuple of (signals, error). On success, error is None. On failure,
        signals is an empty tuple and error is a human-readable message.
    """
    items = [s.strip().lower() for s in (raw or "").split(",") if s.strip()]
    if not items:
        return (), "config `signals` must list at least one of: traces, metrics, logs"
    # Preserve user-supplied order, drop duplicates.
    seen: List[str] = []
    for item in items:
        if item not in VALID_SIGNALS:
            return (), f"config `signals` contains unknown signal {item!r}"
        if item not in seen:
            seen.append(item)
    return tuple(seen), None


def _validate_duration(raw: str) -> Optional[str]:
    """Validate the `duration` config option.

    Returns None on success, or a human-readable error message on failure.
    """
    if not raw:
        return "config `duration` must not be empty (use `inf` for unbounded generation)"
    if not _DURATION_RE.match(raw):
        return (
            f"config `duration` is not a valid Go duration string: {raw!r}"
            " (examples: `30s`, `5m`, `1h30m`, or `inf`)"
        )
    return None


def _validate_numeric(rate: float, workers: int) -> Optional[str]:
    """Validate `rate` and `workers` are non-negative.

    Returns None on success, or a human-readable error message on failure.
    `rate=0` is allowed (telemetrygen treats it as "no throttling").
    `workers=0` is not allowed (it would silently produce nothing).
    """
    if rate < 0:
        return f"config `rate` must be non-negative (got {rate})"
    if workers < 1:
        return f"config `workers` must be at least 1 (got {workers})"
    return None


def _strip_scheme(endpoint: str) -> Tuple[Optional[str], Optional[str]]:
    """Strip any `http(s)://` scheme and validate that a port is present.

    The OTLP relation library may publish either a bare `host:port` (typical
    for gRPC) or a `scheme://host:port` (typical for HTTP). The
    `telemetrygen` `--otlp-endpoint` flag always wants the bare `host:port`
    form; without an explicit port telemetrygen falls back to port 0.

    Returns:
        A tuple of (host_port, error). On success, error is None. On
        failure, host_port is None and error is a human-readable message.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        host_port = parsed.netloc
    else:
        host_port = endpoint

    # urlparse treats a bare `host:port` as scheme=`host`, so we have to
    # look for the port the hard way: there must be a `:` after any `]`
    # (closing bracket for an IPv6 literal) and the trailing component
    # must be all-digits.
    closing_bracket = host_port.rfind("]")
    colon = host_port.rfind(":")
    if colon == -1 or colon < closing_bracket:
        return None, (
            f"OTLP endpoint {endpoint!r} is missing an explicit `:port`"
            " — telemetrygen requires `host:port`"
        )
    port = host_port[colon + 1 :]
    if not port.isdigit():
        return None, (f"OTLP endpoint {endpoint!r} has a non-numeric port {port!r}")
    return host_port, None


def _build_command(
    signal: str,
    state: _DesiredState,
    endpoint: OtlpEndpoint,
    host_port: str,
) -> str:
    """Render the `telemetrygen <signal> ...` command line for Pebble.

    Pebble accepts a single command string per service. We construct the args
    explicitly rather than via shell quoting tricks: every argument value
    comes from typed charm config or the validated OTLP endpoint, so there is
    no untrusted shell input.
    """
    args: List[str] = [
        TELEMETRYGEN_BINARY,
        signal,
        f"--otlp-endpoint={host_port}",
        f"--duration={state.duration}",
        f"--rate={state.rate}",
        f"--workers={state.workers}",
        f"--service={state.service_name}-{signal}",
    ]
    if endpoint.protocol == "http":
        args.append("--otlp-http")
    if endpoint.insecure:
        args.append("--otlp-insecure")
    return " ".join(args)


def _service_name(relation_id: int, signal: str) -> str:
    """Pebble service name for a given (relation, signal) pair.

    Format: `telemetrygen-<relation_id>-<signal>`. The relation id keeps
    services from different remotes from colliding when the charm fans out
    to multiple OTLP receivers.
    """
    return f"{SERVICE_PREFIX}{relation_id}-{signal}"


# --- Charm --------------------------------------------------------------------


class TelemetrygenCharm(ops.CharmBase):
    """Drive the upstream telemetrygen CLI off an OTLP relation."""

    def __init__(self, *args):
        super().__init__(*args)

        # OTLP requirer. We declare support for both transports and all three
        # signals; the library picks the best endpoint per relation (gRPC
        # preferred over HTTP) and we then intersect with the user-selected
        # signals when building Pebble commands.
        #
        # We intentionally do not call `self._otlp.publish()`: the generator
        # produces synthetic data and has no alert rules / juju topology to
        # forward to the provider. If we ever ship bundled alert rules,
        # revisit this.
        self._otlp = OtlpRequirer(
            self,
            relation_name=OTLP_RELATION,
            protocols=["grpc", "http"],
            telemetries=["traces", "metrics", "logs"],
        )

        # Status reporting must be registered before the reconcile observer
        # so that `collect_unit_status` runs *after* reconcile within the
        # same Juju event, surfacing the freshest status to the user.
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # One reconciler. Every observed event funnels through here.
        observe_events(self, reconcilable_events_k8s, self._reconcile)
        # `reconcilable_events_k8s` deliberately excludes UpgradeCharmEvent
        # (the upstream comment: "this is your only chance to know you've
        # been upgraded"). We do not need a separate upgrade hook, but we
        # *do* still want to reconverge the workload after an upgrade in
        # case the layer-rendering logic changed.
        self.framework.observe(self.on.upgrade_charm, self._reconcile)

    # --- Properties ---------------------------------------------------------

    @property
    def _container(self) -> ops.Container:
        return self.unit.get_container(WORKLOAD_CONTAINER)

    def _desired_state(self) -> _DesiredState:
        """Snapshot config + relation data into a single value.

        Pulling everything once at the top of reconcile guarantees the rest of
        the flow sees a consistent view, and makes the reconcile function
        easy to test by parameterising the snapshot directly.
        """
        configured_signals, config_error = _parse_signals(str(self.config.get("signals", "")))
        rate = float(self.config.get("rate", 1.0))
        workers = int(self.config.get("workers", 1))
        duration = str(self.config.get("duration", "inf"))
        service_name = str(self.config.get("service_name", "telemetrygen"))

        # First-error wins to keep the BlockedStatus message focused; the
        # user will fix one mistake at a time anyway.
        if config_error is None:
            config_error = _validate_duration(duration)
        if config_error is None:
            config_error = _validate_numeric(rate, workers)

        targets: List[_RelationTarget] = []
        mismatches: List[str] = []
        configured_set = set(configured_signals)

        if config_error is None and configured_signals:
            for relation_id, endpoint in self._otlp.endpoints.items():
                advertised = set(endpoint.telemetries)
                intersection = tuple(s for s in configured_signals if s in advertised)
                if not intersection:
                    mismatches.append(
                        f"relation {relation_id}: provider advertises"
                        f" {sorted(advertised) or 'no signals'}, none overlap with"
                        f" configured {list(configured_signals)}"
                    )
                    continue
                if set(intersection) != configured_set:
                    dropped = sorted(configured_set - set(intersection))
                    logger.warning(
                        "relation %s: provider does not advertise %s; continuing with %s",
                        relation_id,
                        dropped,
                        list(intersection),
                    )

                host_port, port_error = _strip_scheme(endpoint.endpoint)
                if host_port is None:
                    # Treat as a per-relation problem, not a global config
                    # error — other relations may still be usable.
                    mismatches.append(f"relation {relation_id}: {port_error}")
                    continue

                targets.append(
                    _RelationTarget(
                        relation_id=relation_id,
                        endpoint=endpoint,
                        signals=intersection,
                    )
                )

        return _DesiredState(
            configured_signals=configured_signals,
            rate=rate,
            duration=duration,
            workers=workers,
            service_name=service_name,
            targets=tuple(targets),
            config_error=config_error,
            signal_mismatches=tuple(mismatches),
        )

    # --- Reconciler ---------------------------------------------------------

    def _reconcile(self, _event: ops.EventBase) -> None:
        """Converge the workload toward the desired state.

        The reconcile pipeline:
          1. Snapshot config + relation data.
          2. If the workload container is not yet reachable, do nothing —
             we will be re-invoked on `pebble-ready`.
          3. Build the Pebble layer from every `(relation, signal)` target
             whose provider advertises the configured signal(s). If no
             targets remain (no relation, invalid config, no signal
             overlap), tear down any running services so the unit cannot
             silently emit stale telemetry. Status is reported separately
             via `collect_unit_status`.
          4. Otherwise diff-apply the layer.
        """
        if not self._container.can_connect():
            logger.debug("workload container not yet reachable; skipping reconcile")
            return

        state = self._desired_state()

        if not state.targets:
            self._teardown_services()
            return

        self._apply_layer(state)

    # --- Layer management ---------------------------------------------------

    def _build_services(self, state: _DesiredState) -> Dict[str, ops.pebble.ServiceDict]:
        """Build the desired Pebble service dict for the current state."""
        services: Dict[str, ops.pebble.ServiceDict] = {}
        for target in state.targets:
            # `_strip_scheme` already validated this target's endpoint.
            host_port, _ = _strip_scheme(target.endpoint.endpoint)
            assert host_port is not None  # noqa: S101  filtered out above
            for signal in target.signals:
                service: ops.pebble.ServiceDict = {
                    "override": "replace",
                    "summary": f"telemetrygen {signal} -> rel {target.relation_id}",
                    "command": _build_command(signal, state, target.endpoint, host_port),
                    "startup": "enabled",
                    # `telemetrygen` exits cleanly when `--duration` elapses
                    # or when it has finished sending a `--<signal>` count.
                    # Restart on success so a finite duration produces a
                    # continuous load in batches; restart on failure too so
                    # transient endpoint blips don't terminate the unit.
                    "on-success": "restart",
                    "on-failure": "restart",
                }
                services[_service_name(target.relation_id, signal)] = service
        return services

    def _build_layer(self, state: _DesiredState) -> ops.pebble.Layer:
        """Construct the Pebble layer for the current desired state."""
        layer_dict: ops.pebble.LayerDict = {
            "summary": "telemetrygen layer",
            "description": "Pebble layer for the OpenTelemetry telemetrygen CLI.",
            "services": self._build_services(state),
        }
        return ops.pebble.Layer(layer_dict)

    def _apply_layer(self, state: _DesiredState) -> None:
        """Diff and apply the Pebble layer, then restart changed services.

        Pebble's plan is layered: every `add_layer(..., combine=True)` merges
        on top, and once a service name appears in the plan it cannot be
        removed (only redefined). That means orphaned services (e.g. the user
        narrowed `signals` or dropped a relation) live forever in the plan.
        To avoid an infinite replan loop, we:

          1. Stop any orphaned (managed-prefix) services that are still
             running, so we don't keep emitting stale telemetry.
          2. Diff *only* the managed services we currently want against the
             current plan (filtered to the same prefix). Orphans that have
             been stopped are excluded from the diff, so the next reconcile
             converges cleanly.
        """
        desired = self._build_layer(state)
        desired_services = desired.to_dict().get("services", {})
        current_services = self._container.get_plan().to_dict().get("services", {})

        # Stop orphans first so they don't get auto-restarted by the
        # `on-success: restart` policy after the replan below.
        for name in current_services:
            if not name.startswith(SERVICE_PREFIX) or name in desired_services:
                continue
            try:
                if self._container.get_service(name).is_running():
                    self._container.stop(name)
            except ops.ModelError:
                # Service may have already disappeared from the plan
                # between the get_plan() call and now.
                pass

        # Diff only the services this charm owns. Orphans are intentionally
        # excluded so they don't keep flagging the plan as drifted.
        managed_current = {
            name: spec
            for name, spec in current_services.items()
            if name.startswith(SERVICE_PREFIX) and name in desired_services
        }
        if managed_current == desired_services:
            logger.debug("pebble plan unchanged; no replan needed")
            return

        self._container.add_layer(WORKLOAD_CONTAINER, desired, combine=True)
        # `replan` will (re)start anything not already running with the new
        # command. Any service whose command changed will be restarted by
        # Pebble itself thanks to `override: replace`.
        self._container.replan()

    def _teardown_services(self) -> None:
        """Stop all telemetrygen Pebble services managed by this charm.

        Used when the charm transitions to a non-Active state (missing
        relation, invalid config, no signal overlap). Stopped services
        remain in the plan until the next successful reconcile replaces
        them, but `on-success`/`on-failure` are set to `restart` so we
        explicitly stop them here rather than letting Pebble re-bring
        them up.
        """
        try:
            services = self._container.get_services()
        except ops.pebble.ConnectionError:
            return
        for name, info in services.items():
            if name.startswith(SERVICE_PREFIX) and info.is_running():
                self._container.stop(name)

    # --- Status -------------------------------------------------------------

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        """Report unit status based on the current desired state.

        This handler is intentionally read-only: it inspects state but does
        not mutate it. All convergence happens in `_reconcile`.
        """
        if not self._container.can_connect():
            event.add_status(ops.WaitingStatus("waiting for telemetrygen container"))
            return

        state = self._desired_state()

        if state.config_error:
            event.add_status(ops.BlockedStatus(state.config_error))
            return

        if not self.model.relations.get(OTLP_RELATION):
            event.add_status(ops.BlockedStatus(f"missing required relation: {OTLP_RELATION}"))
            return

        if not self._otlp.endpoints:
            event.add_status(ops.WaitingStatus(f"waiting for OTLP endpoint on `{OTLP_RELATION}`"))
            return

        if not state.targets:
            # Every related provider advertised no overlap with the
            # configured signals. Surface the full picture so the operator
            # knows exactly which side to fix.
            event.add_status(
                ops.BlockedStatus(
                    "OTLP provider(s) do not advertise any of the configured"
                    f" signals {list(state.configured_signals)}:"
                    f" {'; '.join(state.signal_mismatches)}"
                )
            )
            return

        event.add_status(ops.ActiveStatus(_active_message(state)))


def _active_message(state: _DesiredState) -> str:
    """Human-friendly Active status message."""
    if len(state.targets) == 1:
        target = state.targets[0]
        joined: Iterable[str] = target.signals
        return (
            f"generating {', '.join(joined)} via OTLP/{target.endpoint.protocol}"
            f" -> {target.endpoint.endpoint}"
        )
    return (
        f"generating telemetry to {len(state.targets)} OTLP receivers"
        f" (configured signals: {', '.join(state.configured_signals)})"
    )


if __name__ == "__main__":
    ops.main(TelemetrygenCharm)
