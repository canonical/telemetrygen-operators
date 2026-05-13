# Integration test rewrite — handoff

The user wants the integration tests at `tests/integration/` rewritten to a higher quality bar, then actually run and passing. They are taking care of running tests themselves — you produce the code.

## What you're inheriting

A partial BDD-style integration test layout already exists in `tests/integration/`:

```
tests/integration/
├── conftest.py
├── helpers.py
├── features/
│   ├── happy_path.feature
│   ├── invalid_config.feature
│   ├── relation_departed.feature
│   └── signal_switch.feature
├── test_happy_path.py
├── test_invalid_config.py
├── test_relation_departed.py
└── test_signal_switch.py
```

These were written by an earlier agent. They look structurally reasonable but have not been executed end-to-end. The user reports the integration tests "fail" — you need to actually run them, identify failures, and fix them.

The pre-existing happy path file (`tests/integration/test_integration.py`) has been removed; the current `tests/unit/` has 27/27 passing and is out of scope here.

## User's explicit gripes about the previous state

Selected via AskUserQuestion earlier this session:

1. **`try: import jubilant / except: pytest.skip` dance** — and the `_build_charm() pytest.skip` shortcut. Tests that quietly skip never break. Must fail loudly when the env isn't ready. The current `conftest.py`/`helpers.py` already drops these — verify they're truly gone everywhere.
2. **Missing failure-mode coverage** — only the happy path was tested before. Coverage needed: invalid config → Blocked, relation broken → Blocked, switching signals while related. The current scaffolds attempt to cover these; verify they actually exercise the behavior.
3. **No feature files** — `pytest-bdd` was in dev deps but no `.feature` files existed. The scaffold now has four feature files; verify Gherkin reads naturally to a human.

## Decisions the user already made

- **Receiver**: use `opentelemetry-collector-k8s` from the **`dev/edge`** channel. User says it exposes `receive-otlp` with interface `otlp` there. The current `helpers.py` already pins `COLLECTOR_CHANNEL = "dev/edge"`. Trust this unless you find concrete evidence the channel doesn't expose the interface — in which case stop and report rather than falling back to a stub.
- **Model lifecycle**: destroy any existing `telemetrygen-test` model first, then use `jubilant.temp_model()`. `conftest.py` already has `_destroy_legacy_models()` running in `pytest_configure` — verify it's correct.

## Known build-side gotchas

1. **`src/charm.py` must be executable inside the packed charm.** Charmcraft 4.x strips the +x bit. The current `helpers._ensure_charm_py_executable` patches the zip in place on every test run. Don't remove this unless `charmcraft pack` reliably preserves the mode bit.
2. **`lib/charms/__init__.py` was a spurious empty file** that broke `charmcraft pack` with `NotADirectoryError`. Manual-deploy agent removed it; verify it's still gone.
3. **OCI image must be passed via `resources={"telemetrygen-image": ...}`** in the jubilant `deploy()` call. `upstream-source` in `charmcraft.yaml` is only consumed by Charmhub publishing. Already wired in `conftest.py`.

## Known charm-side bug (don't try to fix — out of scope)

`_apply_layer` has a recovery bug: after `_teardown_services()` stops services because of invalid config, fixing the config doesn't restart them, because the resulting Pebble plan diff is unchanged so `replan()` is skipped. The `test_invalid_config` scenario asserts return-to-Active after fixing the config, which may flake against this bug. If it fails:

- Don't patch `src/charm.py`. Report it and either xfail the "returns to Active" step with a clear reason, or weaken the assertion to "the unit is no longer Blocked" (still useful, doesn't trigger the bug).
- The unit tests already enforce the spec; integration tests are about real-world behavior.

## Constraints

- **Do not modify** `src/charm.py`, `charmcraft.yaml`, `requirements.txt`, `pyproject.toml`, `tests/unit/`. If you find a real bug blocking the integration tests there, stop and report.
- You **may modify** anything under `tests/integration/`, plus `tox.ini` and `unit.requirements.txt`.
- Bypass-permissions is ON in this session. All Bash is auto-approved.
- juju 3.6.21, charmcraft 4.0.1, microk8s available. The user has a bootstrapped k8s controller.

## Recommended workflow

1. Read `tests/integration/conftest.py` and `tests/integration/helpers.py` first — that's where most of the setup logic lives.
2. Read each `tests/integration/test_*.py` + matching `features/*.feature` pair. Look for:
   - Gherkin that reads like jargon, not a user story
   - Step bindings that no-op when they shouldn't
   - Assertions that pass on a half-broken system
3. Pack the charm: `cd /home/dylan/demo/telemetrygen-k8s && charmcraft pack` (chmod the result if needed; helpers already does this in-zip but verify).
4. Run: `tox -e integration`. Iterate on failures.
5. The `helpers.assert_traces_received_by_collector` function polls otelcol's workload logs for received-batch markers. If `dev/edge` of `opentelemetry-collector-k8s` doesn't actually expose `debug_exporter_for_traces` / etc. as config options, you'll need to either pick a different verification method (otelcol's internal `_total` metrics, jaeger query, etc.) or hand the user a clear failure with what's needed.

## Files in scope

- `/home/dylan/demo/telemetrygen-k8s/tests/integration/conftest.py`
- `/home/dylan/demo/telemetrygen-k8s/tests/integration/helpers.py`
- `/home/dylan/demo/telemetrygen-k8s/tests/integration/features/*.feature`
- `/home/dylan/demo/telemetrygen-k8s/tests/integration/test_*.py`
- `/home/dylan/demo/telemetrygen-k8s/tox.ini` (may need tweaks for env, deps)

## Deliverable for the next agent

- `tox -e integration` passes end-to-end on a real microk8s + juju 3.6 controller (the user runs the tests themselves; produce code that you have reasonable confidence will pass).
- Summary: what was broken, what changed, list of scenarios in the feature files (one-liners), known flakes, any open issues hand-flagged to the user.

## Provenance / breadcrumbs

This session built the charm in phases:
- Implementation: Charm Architect (opus)
- Unit tests: sonnet (claude subagent type) — passed 27/27, 93% coverage
- Code review: Charm Architect (opus) — surfaced 3 P0 + 6 P1
- Fix pass: Charm Architect (opus) — applied all P0/P1, documented behavior changes
- Test reconciliation: sonnet — updated unit tests for behavior changes, all green
- Manual deploy: sonnet — verified 334 spans flowed through to otelcol; surfaced build hygiene issues (chmod +x, lib/charms/__init__.py)

The integration test scaffold present today is unattributed in this thread — assume it's a half-finished attempt and treat it as a starting point, not a finished product.

Task #7 in the harness task list ("Rewrite integration tests properly") was the most recent in-progress task when this handoff was written.
