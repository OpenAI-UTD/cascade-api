# Target Workload Demo

Cascade is the reliability platform. Sock Shop is the canonical target workload that Cascade observes, investigates, and safely targets with dry-run chaos and remediation workflows.

Sock Shop manifests live under `targets/sock-shop/` and deploy into the shared target namespace `cascade-targets`.

## Deploy Sock Shop

```powershell
.\scripts\deploy-targets.ps1
```

The script applies `targets/sock-shop` and waits for the target deployments to roll out.

## Verify Target Resources

```powershell
kubectl get pods -n cascade-targets
kubectl get svc -n cascade-targets
kubectl get deploy -n cascade-targets
```

Expected target services include `front-end`, `catalogue`, `catalogue-db`, `carts`, `carts-db`, `orders`, `orders-db`, `payment`, `shipping`, `queue-master`, `rabbitmq`, `user`, and `user-db`.

## Open Sock Shop Locally

```powershell
kubectl port-forward -n cascade-targets svc/front-end 18099:80
```

Open `http://localhost:18099`.

## Generate Demo Traffic

With the port-forward running:

```powershell
1..60 | ForEach-Object {
  Invoke-WebRequest -UseBasicParsing http://localhost:18099/ | Out-Null
  Invoke-WebRequest -UseBasicParsing http://localhost:18099/category.html | Out-Null
  Start-Sleep -Milliseconds 500
}
```

## Prometheus Checks

Use these queries to confirm Kubernetes target signals are present:

```promql
kube_pod_status_phase{namespace="cascade-targets"}
sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="cascade-targets", container!="", pod!=""}[5m]))
sum by (pod) (container_memory_working_set_bytes{namespace="cascade-targets", container!="", pod!=""})
sum by (pod) (kube_pod_container_status_restarts_total{namespace="cascade-targets", container!="", pod!=""})
```

## Validate Cascade Observation

Run the normal phase acceptance checks after target deployment:

```powershell
.\scripts\accept-telemetry.ps1
.\scripts\accept-anomaly-detection.ps1
.\scripts\accept-chaos.ps1 -DryRunOnly
.\scripts\accept-remediation.ps1
.\scripts\accept.ps1
.\scripts\accept-all.ps1
```

Telemetry pipeline proves telemetry flow, Anomaly detection proves anomaly detection, Chaos engineering proves safe chaos dry-runs against the target, Remediation proves remediation planning and dry-run validation, and Command Center UI proves the Command Center path.

## Safe Chaos Dry-Run

The normal demo target for chaos is `catalogue` in namespace `cascade-targets`. Keep demos in dry-run mode:

```powershell
.\scripts\accept-chaos.ps1 -DryRunOnly
```

The Command Center UI also exposes dry-run planning and dry-run execution only by default.

## Optional Local Live Actions

Real action demos are scripts-only and require explicit local-kind confirmation:

```powershell
.\scripts\install-chaos-mesh.ps1 -ConfirmLocalKind
.\scripts\demo-real-chaos.ps1 -ConfirmLocalKind
.\scripts\demo-real-remediation.ps1 -ConfirmLocalKind
```

The live path is bounded to `cascade-targets` and safe Sock Shop services. It blocks databases, brokers, session stores, `cascade-system`, wildcard selectors, namespace deletion, and deployment deletion. Use it only on your own local demo cluster.

## Command Center Demo

```powershell
kubectl port-forward -n cascade-system svc/command-center 18300:8030
```

Open `http://localhost:18300`.

Recommended demo flow:

1. Use Sock Shop traffic to create fresh target activity.
2. Open Command Center Overview and confirm counts update.
3. Inspect `/telemetry` for target service events.
4. Inspect `/anomalies` and start a deterministic investigation if signals exist.
5. Search `/knowledge` for a runbook, for example `catalogue latency runbook`.
6. Use `/chaos` to create a dry-run plan against `catalogue`.
7. Use `/remediation` to create a safe plan, record approval, and run dry-run validation.
8. Use `/system` to confirm backend health.

## Legacy Online Boutique Source

`targets/online-boutique-src/` is an empty, fetch-on-demand placeholder (the old submodule-style gitlink had no `.gitmodules`, so fresh clones got an empty directory). Fetch instructions live in `targets/online-boutique-src/README.md`. Do not deploy it for current Cascade demos unless you are deliberately testing legacy compatibility.
