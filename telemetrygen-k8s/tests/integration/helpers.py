# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants and helpers for telemetrygen-k8s integration tests."""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Iterable

import jubilant
import tenacity

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

# Telemetrygen application name as deployed into the test model.
TELEMETRYGEN_APP = "telemetrygen-k8s"

# OTLP receiver application name.
COLLECTOR_APP = "otelcol"

# Charm-side relation name on the requirer side (telemetrygen-k8s).
TELEMETRYGEN_OTLP_ENDPOINT = "send-otlp"

# Relation name on the provider side (opentelemetry-collector-k8s).
COLLECTOR_OTLP_ENDPOINT = "receive-otlp"

# OCI image for the telemetrygen workload. `upstream-source` in charmcraft.yaml
# is only consumed by Charmhub-side publishing; local-pack deploys must pass
# this image explicitly via `resources={...}`.
TELEMETRYGEN_IMAGE = "ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.152.0"

# Receiver published on Charmhub.  dev/edge has the modern unified `otlp`
# relation that telemetrygen-k8s targets (verified by inspecting the packed
# charm's metadata.yaml — `juju info`'s relations summary is stale).
COLLECTOR_CHARM = "opentelemetry-collector-k8s"
COLLECTOR_CHANNEL = "dev/edge"

# Path to the locally-packed telemetrygen-k8s charm. Supplied by CI via
# CHARM_PATH; we deliberately do NOT have a fallback that silently runs
# without an artifact — see conftest.py.
CHARM_PATH_ENV = "CHARM_PATH"

# Workload container name in telemetrygen-k8s.
TELEMETRYGEN_CONTAINER = "telemetrygen"

# Workload container name in opentelemetry-collector-k8s.
COLLECTOR_CONTAINER = "otelcol"

# --- Helpers -----------------------------------------------------------------


def charm_path() -> pathlib.Path:
    """Return the path to the packed telemetrygen-k8s charm.

    Falls back to any `*.charm` file in the repo root if CHARM_PATH is unset,
    but raises (does not skip) if no artifact can be located. Integration
    tests that can't find a charm must fail loudly — silent skips hide real
    breakage.

    Side-effect: ensures `src/charm.py` inside the packed charm has the
    executable bit set. Charmcraft 4.x strips the +x mode on file entries
    even though the source tree has it; the resulting charm fails `install`
    with `Permission denied`. This is pure build hygiene — we do not touch
    the charm code, only the zip entry's mode bits.
    """
    env = os.environ.get(CHARM_PATH_ENV)
    if env:
        p = pathlib.Path(env).resolve()
        if not p.exists():
            raise FileNotFoundError(
                f"{CHARM_PATH_ENV}={env!r} but no file at that path. "
                "Build the charm with `charmcraft pack` first."
            )
    else:
        # Fall back to repo-root *.charm (handy for local iteration). We do
        # not invoke `charmcraft pack` from here — packing inside integration
        # tests makes the test runner do double-duty and slows down per-file
        # runners in CI. Pack once in CI, point CHARM_PATH at the artifact.
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        candidates = sorted(repo_root.glob("*.charm"))
        if not candidates:
            raise FileNotFoundError(
                f"No charm artifact found. Set {CHARM_PATH_ENV} or run "
                f"`charmcraft pack` in {repo_root}."
            )
        p = candidates[-1]
        logger.info("Using locally-built charm %s (CHARM_PATH not set)", p)

    _ensure_charm_py_executable(p)
    return p


def _ensure_charm_py_executable(charm: pathlib.Path) -> None:
    """Set the +x bit on `src/charm.py` inside the packed charm if missing.

    Charmcraft 4.x re-stages files and loses the executable bit. The
    `dispatch` shim does `exec ./src/charm.py`, which fails with
    `Permission denied` if the bit isn't set. We rewrite the zip in
    place rather than re-running charmcraft because re-packing inside
    the test runner violates the "pack once" rule.
    """
    import shutil
    import zipfile

    with zipfile.ZipFile(charm, "r") as z:
        info = z.getinfo("src/charm.py")
        mode = (info.external_attr >> 16) & 0o7777
        if mode & 0o111:
            return  # already executable
        logger.info(
            "patching `src/charm.py` mode in %s: %s -> 0o755 (build hygiene)",
            charm,
            oct(mode),
        )

    tmp = charm.with_suffix(charm.suffix + ".tmp")
    with (
        zipfile.ZipFile(charm, "r") as zin,
        zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "src/charm.py":
                new_mode = ((item.external_attr >> 16) & 0o7777) | 0o755
                item.external_attr = (new_mode << 16) | (item.external_attr & 0xFFFF)
            zout.writestr(item, data)
    shutil.move(tmp, charm)


def _pebble_services(juju: jubilant.Juju, unit: str, container: str) -> str:
    """Return the raw `pebble services` output for *container* on *unit*.

    We deliberately do NOT `juju ssh --container <name>`: the telemetrygen
    workload image is a scratch image with no shell, so `juju ssh` into it
    fails with `exec: "sh": executable file not found in $PATH`. Instead we
    ssh into the charm container (which has bash + the pebble client) and
    point the pebble client at the workload's pebble socket via PEBBLE_SOCKET.
    The socket is shared into /charm/containers/<name>/pebble.socket on
    every k8s sidecar charm.
    """
    return juju.cli(
        "ssh",
        unit,
        f"PEBBLE_SOCKET=/charm/containers/{container}/pebble.socket /charm/bin/pebble services",
    )


def running_services(juju: jubilant.Juju, unit: str, container: str) -> set[str]:
    """Return the set of pebble service names currently in the active state.

    Parses `pebble services` output. Lines look like::

        Service                       Startup  Current  Since
        telemetrygen-1-traces         enabled  active   today at 09:00 UTC
    """
    out = _pebble_services(juju, unit, container)
    running: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        # Skip header. The header column "Service" is title-cased; service
        # names are lowercased so this is a robust filter.
        if parts[0] == "Service":
            continue
        if parts[2].lower() == "active":
            running.add(parts[0])
    return running


def collector_relation_id(juju: jubilant.Juju, app: str = TELEMETRYGEN_APP) -> int:
    """Return the relation id of the unique send-otlp relation for *app*.

    Used to predict the Pebble service name `telemetrygen-<rel-id>-<signal>`.
    `juju status` does not surface relation ids in its parsed output, so we
    drop to `juju show-unit --endpoint send-otlp --format json` and pluck
    the integer id out of the relation-info block.
    """
    import json

    raw = juju.cli(
        "show-unit",
        f"{app}/0",
        "--endpoint",
        TELEMETRYGEN_OTLP_ENDPOINT,
        "--format",
        "json",
    )
    parsed = json.loads(raw)
    unit = parsed[f"{app}/0"]
    for info in unit.get("relation-info", []):
        if info.get("endpoint") == TELEMETRYGEN_OTLP_ENDPOINT:
            return int(info["relation-id"])
    raise AssertionError(
        f"could not locate a {TELEMETRYGEN_OTLP_ENDPOINT!r} relation id for {app}; "
        f"show-unit output:\n{raw}"
    )


def predicted_service_names(relation_id: int, signals: Iterable[str]) -> set[str]:
    """Construct the expected `telemetrygen-<rel-id>-<signal>` set."""
    return {f"telemetrygen-{relation_id}-{s}" for s in signals}


@tenacity.retry(
    stop=tenacity.stop_after_attempt(30),
    wait=tenacity.wait_fixed(5),
    reraise=True,
)
def assert_traces_received_by_collector(
    juju: jubilant.Juju,
    unit: str = f"{COLLECTOR_APP}/0",
) -> None:
    """Assert that the otelcol unit's stdout shows at least one received span.

    The opentelemetry-collector-k8s charm exposes a `debug_exporter_for_traces`
    config option that prints received telemetry to stdout, which Pebble
    captures in the service log. We poll those logs until we see a recognisable
    `debug` exporter banner. tenacity does the polling — telemetrygen sends at
    ~1 record/sec by default, so we may need a few seconds before the first
    flush.
    """
    out = juju.cli(
        "ssh",
        unit,
        f"PEBBLE_SOCKET=/charm/containers/{COLLECTOR_CONTAINER}/pebble.socket"
        " /charm/bin/pebble logs otelcol -n 500",
    )
    # `debug` exporter prints a header line per batch followed by per-resource
    # lines. The exact format depends on the otelcol release we got from
    # dev/edge; on 0.130.x the output looks like::
    #
    #   2025-...  Traces  {"resource spans": 1, "spans": 2}
    #   2025-...  ResourceTraces #0 [...] service.name=telemetrygen-traces
    #
    # Older releases printed `ResourceSpans` instead of `ResourceTraces`.
    # Any of these markers is a positive signal that data reached the
    # receiver and was emitted by the debug exporter.
    if not any(marker in out for marker in ("ResourceTraces", "ResourceSpans", "resource spans")):
        raise AssertionError(
            "no spans observed in otelcol debug exporter output after waiting; "
            f"last 500 lines:\n{out[-2000:]}"
        )
