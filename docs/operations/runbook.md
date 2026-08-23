# Cascade Operations Runbook

Cascade runs locally on kind in the `cascade-system` namespace and observes demo workloads in `cascade-targets`.

## Startup

Deploy phases in order when starting from a fresh cluster:

```powershell
.\scripts\deploy-targets.ps1
.\scripts\deploy-telemetry.ps1
.\scripts\deploy-storage-memory.ps1
.\scripts\deploy-anomaly-detection.ps1
.\scripts\deploy-knowledge-rag.ps1
.\scripts\deploy-agents.ps1
.\scripts\deploy-chaos.ps1
.\scripts\deploy-remediation.ps1
.\scripts\deploy.ps1
```

For a cluster that already has Remediation healthy, Command Center UI can be refreshed directly:

```powershell
.\scripts\deploy.ps1 -SkipRemediationDeploy
```

## Acceptance

```powershell
.\scripts\accept.ps1
.\scripts\accept-all.ps1
```

Use `-ContinueOnFailure` on `accept-all.ps1` when collecting a full failure matrix.

## Access

Command Center:

```powershell
kubectl port-forward -n cascade-system svc/command-center 18300:8030
```

Open `http://localhost:18300`.

Sock Shop target workload:

```powershell
kubectl get pods -n cascade-targets
kubectl get svc -n cascade-targets
kubectl port-forward -n cascade-targets svc/front-end 18099:80
```

Open `http://localhost:18099`. Sock Shop is the canonical observed demo workload for Cascade. The historical Online Boutique source tree placeholder remains under `targets/online-boutique-src/` (fetch instructions in its README), but it is inactive and should not be used for current demos.

Common service forwards:

```powershell
kubectl port-forward -n cascade-system svc/clickhouse 18123:8123
kubectl port-forward -n cascade-system svc/qdrant 16333:6333
kubectl port-forward -n cascade-system svc/retrieval-service 18012:8012
```

## Debugging

Collect a support bundle:

```powershell
.\scripts\debug-all.ps1
```

Phase-specific scripts remain available, including `debug.ps1` for Command Center resources and proxy checks.

## Health Probes Audit

Every Cascade service deployment under `infra/kubernetes/*/deployment.yaml` declares both probes with `httpGet` against a named container port:

- `readinessProbe` -> `GET /ready`
- `livenessProbe` -> `GET /health`

The single exception is `redpanda`: its Kafka port is not HTTP-addressable, so it uses `tcpSocket` probes with a generous `startupProbe` instead.

The checks are enforced offline by two gates:

- `python scripts/validate-k8s-manifests.py` parses every manifest YAML and fails on missing or malformed probes (CI job `k8s-manifest-validate`).
- `tests/test_k8s_probe_health.py` asserts the same contract, including that probe ports resolve to declared container ports.

## Backups

```powershell
.\scripts\backup-clickhouse.ps1
.\scripts\backup-qdrant.ps1
```

Restore scripts are dry-run by default and require `-ConfirmRestore`.

## Safety Notes

- Real remediation execution is disabled by default.
- The Command Center proxy blocks real remediation and real chaos execution unless explicitly reconfigured.
- Chaos engineering acceptance should use `-DryRunOnly` for normal validation.
- Real chaos/remediation demos must use the scripts-only local demo path with `-ConfirmLocalKind`.
- Local rate limiting is in-memory and applies only to the `command-center-api` pod.

## Reset And Cleanup

Use phase reset scripts when a local demo has accumulated too much state:

```powershell
.\scripts\reset-command-center.ps1
.\scripts\reset-remediation.ps1
.\scripts\reset-chaos.ps1
```

Cleanup live demo resources and disable live flags:

```powershell
kubectl -n cascade-targets delete podchaos,networkchaos,stresschaos -l cascade.io/phase=phase7 --ignore-not-found=true
kubectl -n cascade-system set env deployment/chaos-executor-service ENABLE_DANGEROUS_ACTIONS=false ENABLE_REAL_CHAOS=false CASCADE_LIVE_DEMO_MODE=false CASCADE_ACTIVE_CLUSTER_CONTEXT-
kubectl -n cascade-system set env deployment/remediation-executor-service EXECUTION_ENABLED=false ENABLE_DANGEROUS_ACTIONS=false ENABLE_REAL_REMEDIATION=false CASCADE_LIVE_DEMO_MODE=false CASCADE_ACTIVE_CLUSTER_CONTEXT-
```

Repeated demos create additional ClickHouse rows. For latest run views, query by the relevant timestamp field and group by run or plan identifier instead of treating raw transition rows as unique final states.
