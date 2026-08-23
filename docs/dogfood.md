# Dogfooding Cascade With Cascade

Cascade's own observability products (telemetry archiver, anomaly detector) and QA
tooling should run against a live Cascade deployment during chaos labs. This page is
a stub: the bullets below describe the intended exercise. Execution is still pending.

## Scope

- [ ] Deploy a full local kind stack (`scripts/deploy.ps1` / `deploy.sh`), including ClickHouse and Redpanda.
- [ ] Point Cascade's telemetry archiver at Cascade's own services so platform requests land in ClickHouse alongside target telemetry.
- [ ] Enable structured request logs per service (uvicorn access logs or middleware) and verify they are queryable.
- [ ] Run an anomaly detection pass over Cascade-generated metrics during a chaos lab session.
- [ ] Use project-qa / repo-qa-runner against this repository in CI (the `cascade-qa` job already does this; extend to scheduled runs).
- [ ] Record one bounded chaos experiment (`pod_kill` on `catalogue`) while observing whether Cascade detects its own degraded service.

## Structured request logs

- [ ] Agree on a minimal log schema per service: timestamp, level, route, status, latency_ms, actor.
- [ ] Verify logs survive archiver retention windows.

## Success criteria

- Cascade dashboards show Cascade's own traffic without manual queries.
- At least one self-detected anomaly from a chaos lab is triaged through the normal investigation flow.
- Findings feed back into hardening backlog items.
