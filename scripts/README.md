# Cascade Scripts

Project QA entry points:

- `invoke-project-qa.ps1`: submit an already assembled CI/runtime evidence payload.
- `../services/repo-qa-runner/app/main.py`: check out a project, detect its stack, execute approved checks in Docker, submit evidence, and optionally apply and verify documented fixes. See `../docs/repo-qa-runner.md`.

Use these scripts from the repository root unless a script says otherwise.

| Script | Purpose |
|---|---|
| `deploy.ps1` | Deploy all Cascade services to local kind |
| `accept.ps1` | Run end-to-end acceptance validation |
| `demo-final.ps1` | Final safe end-to-end product wiring and demo verification |
| `demo.ps1` | Run the full interactive demo flow |
| `demo-command-center.ps1` | Safe stage-by-stage Command Center demo wrapper |
| `debug.ps1` | Dump logs and diagnostics for all services |
| `accept-all.ps1` | Full suite validation across all components |
| `accept-durability.ps1` | Validate PVCs, topic/collection health, and optional restart survival |
| `debug-all.ps1` | Full diagnostic sweep |
| `ci-local.ps1` | Run CI checks locally |
| `audit-secrets.ps1` | Scan tracked files for leaked secrets |
| `deploy-targets.ps1` | Deploy the canonical Sock Shop target workload |
| `validate-target.ps1` | Read-only readiness validation for a target workload namespace |
| `configure-target.ps1` | Register a target config with running Cascade deployments |
| `ensure-redpanda-topics.ps1` | Ensure required Kafka-compatible Redpanda topics exist |
| `verify-chaos-mesh.ps1` | Verify local Chaos Mesh CRDs/controller and Cascade namespace protections |
| `install-chaos-mesh.ps1` | Install or upgrade Chaos Mesh for explicitly confirmed local kind demos |
| `demo-real-chaos.ps1` | Run opt-in bounded real chaos in local demo mode |
| `demo-chaos-campaign.ps1` | Run a safe dry-run chaos campaign against local kind targets |
| `demo-real-remediation.ps1` | Run opt-in bounded real remediation in local demo mode |
| `deploy-autopilot.ps1` | Deploy the Autopilot closed-loop orchestration service |
| `accept-autopilot.ps1` | Validate Autopilot health, mode, and run listing |
| `deploy-scheduler.ps1` | Deploy the always-on scheduler worker for safe dry-run schedules |
| `accept-scheduler.ps1` | Validate scheduler health, status, item control, and history |
| `backup-cascade-state.ps1` | Back up ClickHouse, Qdrant, and Redpanda topic metadata into one timestamped folder |
| `wipe-cascade-state.ps1` | Explicitly wipe local ClickHouse, Redpanda, and Qdrant PVC state |

## Quick Start

```powershell
.\scripts\deploy.ps1
.\scripts\deploy-targets.ps1
.\scripts\accept.ps1
.\scripts\demo-final.ps1
.\scripts\demo-command-center.ps1 -Stage All
```

Open the Command Center after deployment:

```powershell
kubectl port-forward -n cascade-system svc/command-center 18300:8030
```

Then browse to `http://localhost:18300`.

## Component Helpers

The main scripts are the demo-facing entrypoints. Component-scoped helpers are kept for targeted development and recovery without reintroducing numbered scaffolding:

| Area | Deploy | Accept | Demo | Debug | Reset |
|---|---|---|---|---|---|
| Telemetry pipeline | `deploy-telemetry.ps1` | `accept-telemetry.ps1` | `demo-telemetry.ps1` | - | - |
| Storage and memory | `deploy-storage-memory.ps1` | `accept-storage-memory.ps1` | `demo-storage-memory.ps1` | `debug-storage-memory.ps1` | `reset-storage-memory.ps1` |
| Anomaly detection | `deploy-anomaly-detection.ps1` | `accept-anomaly-detection.ps1` | `demo-anomaly-detection.ps1` | `debug-anomaly-detection.ps1` | `reset-anomaly-detection.ps1` |
| Knowledge and RAG | `deploy-knowledge-rag.ps1` | `accept-knowledge-rag.ps1` | `demo-knowledge-rag.ps1` | `debug-knowledge-rag.ps1` | `reset-knowledge-rag.ps1` |
| Agent investigations | `deploy-agents.ps1` | `accept-agents.ps1` | `demo-agents.ps1` | `debug-agents.ps1` | `reset-agents.ps1` |
| Chaos engineering | `deploy-chaos.ps1` | `accept-chaos.ps1` | `demo-chaos.ps1`, `demo-chaos-campaign.ps1` | `debug-chaos.ps1` | `reset-chaos.ps1` |
| Remediation | `deploy-remediation.ps1` | `accept-remediation.ps1` | `demo-remediation.ps1` | `debug-remediation.ps1` | `reset-remediation.ps1` |
| Autopilot | `deploy-autopilot.ps1` | `accept-autopilot.ps1` | - | - | - |
| Command Center UI | `deploy.ps1` | `accept.ps1` | `demo-command-center.ps1` | `debug.ps1` | `reset-command-center.ps1` |

## Demo Wrapper

`demo-final.ps1` is the final product smoke/demo verifier. It checks cluster prerequisites, required services, target workload readiness, Command Center access, telemetry/topology/RCA APIs, dry-run chaos campaigns, dry-run Autopilot, dry-run remediation, audit history, scheduler status, and route reachability:

```powershell
.\scripts\demo-final.ps1
.\scripts\demo-final.ps1 -RunAcceptAll
```

The script prints Command Center URLs, expected warnings, and a PASS/WARN/FAIL summary. It does not enable live chaos or live remediation.

`demo-command-center.ps1` keeps the presenter flow dry-run-first and labels expected dashboard results:

```powershell
.\scripts\demo-command-center.ps1 -Stage Validate
.\scripts\demo-command-center.ps1 -Stage Telemetry
.\scripts\demo-command-center.ps1 -Stage Chaos
.\scripts\demo-command-center.ps1 -Stage Remediation
.\scripts\demo-command-center.ps1 -Stage Autopilot
.\scripts\demo-command-center.ps1 -Stage Reset
```

It does not enable live chaos or live remediation. Use the explicit `demo-real-*` scripts only for confirmed local-kind live demos.

## Bring Your Own Target

Default safe path:

```powershell
kubectl create namespace cascade-targets --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace cascade-targets cascade.io/monitored=true --overwrite
kubectl apply -n cascade-targets -f path\to\your-app.yaml

.\scripts\validate-target.ps1 -Namespace cascade-targets -ExpectedServices frontend,api -ShowLabels
```

Target config path:

```powershell
Copy-Item -Recurse targets\template targets\my-app
notepad targets\my-app\target.yaml

.\scripts\validate-target.ps1 -Namespace cascade-targets -TargetConfig targets\my-app\target.yaml -Strict
.\scripts\configure-target.ps1 -TargetConfig targets\my-app\target.yaml
.\scripts\ensure-redpanda-topics.ps1
.\scripts\accept-telemetry.ps1
```

`validate-target.ps1` is read-only. `configure-target.ps1` changes Cascade deployment environment and config mounts; use `-DryRun` to preview first.

## Validation And Safety

Run the full local acceptance suite:

```powershell
.\scripts\accept-all.ps1
```

Run local CI checks:

```powershell
.\scripts\ci-local.ps1 -SkipDockerBuild
```

Run secret hygiene before publishing:

```powershell
.\scripts\audit-secrets.ps1
```

Security notes:

- `docs/security.md` explains local mode, production-safe auth, approval binding, RBAC permissions, and ingress/TLS guidance.
- Local demos keep `CASCADE_AUTH_ENABLED=false` unless you explicitly opt into API-key auth.
- If auth is enabled for a shared environment, set `CASCADE_API_KEYS` or `CASCADE_API_KEY_HASHES` and use a bearer token for sensitive Command Center actions.

Useful safe flags:

- `accept-chaos.ps1` skips live failure injection by default; pass `-IncludeLiveChaos` only for authorized local/dev/staging validation.
- `-DryRunOnly` keeps chaos validation non-disruptive explicitly.
- `validate-target.ps1 -Strict` exits nonzero when readiness checks fail.
- `configure-target.ps1 -DryRun` previews target registration without changing deployments.
- `-NoBrowser` prevents demo scripts from opening a browser.
- `-ConfirmRestore` is required before restore scripts mutate local data stores.
- `reset-storage-memory.ps1` keeps PVC data by default; pass `-WipePersistentData` only for a ClickHouse/Qdrant storage reset.
- `wipe-cascade-state.ps1 -ConfirmWipe` is the explicit full local state wipe for ClickHouse, Redpanda, and Qdrant.
- `accept-durability.ps1 -SkipRestart` checks durable wiring without restarting stateful pods.
- `-SkipDockerBuild` skips Docker image smoke builds in local CI.

## Optional Live Local-Demo Path

Dry-run acceptance is the default. The following scripts are for explicitly confirmed local-kind resilience testing only:

```powershell
.\scripts\install-chaos-mesh.ps1 -ConfirmLocalKind
.\scripts\verify-chaos-mesh.ps1
.\scripts\demo-real-chaos.ps1 -ConfirmLocalKind
.\scripts\demo-real-remediation.ps1 -ConfirmLocalKind
```

Live controlled failure injection and live remediation remain opt-in, policy-gated, and unsuitable for production.

Backups and restores:

- `backup-clickhouse.ps1`, `list-clickhouse-backups.ps1`, `restore-clickhouse.ps1`
- `backup-qdrant.ps1`, `list-qdrant-backups.ps1`, `restore-qdrant.ps1`
- `backup-cascade-state.ps1`
- `wipe-cascade-state.ps1`

Generated logs, support bundles, backups, local `.env` files, kubeconfigs, and database volumes are ignored by `.gitignore` and should not be committed.
