# telemetrygen-k8s

A Juju Kubernetes charm that wraps the upstream OpenTelemetry
[`telemetrygen`](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/cmd/telemetrygen)
CLI. Use it to generate synthetic traces, metrics, and/or logs and ship them
over OTLP to a related OTLP receiver in the Canonical Observability Stack
(COS), so you can end-to-end exercise the pipeline (collector → backends →
Grafana) without instrumenting a real application.

The charm follows the COS reconciler convention: every observed event funnels
through a single `_reconcile` method that converges the workload toward the
configured desired state.

## OTLP relation, not legacy ones

This charm speaks only the modern **`otlp`** relation interface, implemented
by [`charmlibs.interfaces.otlp`](https://github.com/canonical/charmlibs/tree/main/interfaces/otlp).
A single relation (`send-otlp`) carries traces, metrics, and logs over either
gRPC (preferred) or HTTP. The charm does **not** use the legacy
`loki_push_api`, `prometheus_scrape`, or `prometheus_remote_write` relations
for this purpose — those are signal-specific and predate the unified OTLP
interface.

The natural counterpart is
[`opentelemetry-collector-k8s`](https://charmhub.io/opentelemetry-collector-k8s),
which exposes `receive-otlp` over the same interface.

## Deploy

```bash
# Deploy the otelcol that will receive the synthetic telemetry.
juju deploy opentelemetry-collector-k8s otelcol

# Deploy this charm.
juju deploy telemetrygen-k8s

# Connect them — traces, metrics, and logs all flow over this one relation.
juju integrate telemetrygen-k8s:send-otlp otelcol:receive-otlp
```

By default the charm generates only `traces` at 1 trace/sec/worker for an
infinite duration. To generate all three signals concurrently:

```bash
juju config telemetrygen-k8s signals=traces,metrics,logs rate=5 workers=2
```

## Configuration

| Option | Default | Meaning |
| --- | --- | --- |
| `signals` | `traces` | Comma-separated list of signals to generate concurrently. Any subset of `traces`, `metrics`, `logs`. One Pebble service is started per signal. |
| `rate` | `1.0` | Approximate items per second, per worker. `0` disables throttling. |
| `duration` | `inf` | Go duration string (`30s`, `5m`, …) or `inf` for unbounded generation. Applies to every signal. |
| `workers` | `1` | Number of concurrent telemetrygen worker goroutines per signal. |
| `service_name` | `telemetrygen` | Value of the `service.name` resource attribute. The signal is appended (e.g. `telemetrygen-traces`). |

### `duration` × restart-policy matrix

`telemetrygen` exits cleanly once `--duration` elapses. The charm sets the
Pebble service restart policy to `on-success: restart` / `on-failure: restart`,
so the precise behaviour depends on the duration you pick:

| `duration` | What happens |
| --- | --- |
| `inf` (default) | One long-running telemetrygen process per `(relation, signal)`. The process never exits voluntarily. Pebble only restarts it if it crashes. This is the most efficient mode and is what you want for "keep my pipeline busy" demos and soak tests. |
| Finite (e.g. `30s`, `5m`, `1h`) | telemetrygen exits cleanly when the duration elapses. Pebble immediately restarts it because `on-success: restart` is set, producing a continuous *stream of batches*. There is a small gap between batches while Pebble restarts the service — useful if you want to exercise the receiver's behaviour at the start/end of a stream, or to see periodic spikes rather than a flat load. |
| Workload crash | Pebble restarts the service via `on-failure: restart`, so a transient OTLP receiver outage will not leave the unit silent once the receiver returns. |

If you only want a single batch and then quiet (no automatic restart), this
charm is not the right tool — use a `juju run` action or a one-shot Job
instead. The charm is opinionated about continuous load.

The `inf` literal is recognised by telemetrygen itself; any other value must
be a Go `time.Duration` string (`30s`, `5m`, `1h30m`, …). Invalid durations
are rejected at config-set time with a BlockedStatus.

## Status semantics

- `BlockedStatus("missing required relation: send-otlp")` — no OTLP receiver related yet.
- `BlockedStatus("config `signals` …")` — `signals` config is empty or contains an unknown value.
- `WaitingStatus("waiting for telemetrygen container")` — Pebble not yet reachable.
- `WaitingStatus("waiting for OTLP endpoint on `send-otlp`")` — relation is up but the provider hasn't published an endpoint yet.
- `ActiveStatus("generating <signals> via OTLP/<protocol> -> <endpoint>")` — workload running.

## Development

```bash
tox -e lint     # ruff + codespell
tox -e static   # pyright
tox -e unit     # unit tests
tox -e integration  # jubilant-driven integration tests (requires juju + microk8s)
```
