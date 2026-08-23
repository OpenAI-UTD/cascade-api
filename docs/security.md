# Cascade Security

Cascade is a local, production-inspired reliability platform. The default repository workflow is optimized for safe local demos: dry-run first, real chaos/remediation disabled, and bounded live-demo execution only when explicitly enabled on a local/dev/staging cluster.

This document describes the production-safe foundation and the remaining responsibilities before exposing Cascade beyond a trusted local operator.

## Local Mode

Local mode keeps developer friction low:

- `CASCADE_AUTH_ENABLED=false`
- `ENABLE_DANGEROUS_ACTIONS=false`
- `ENABLE_REAL_CHAOS=false`
- `ENABLE_REAL_REMEDIATION=false`
- `EXECUTION_ENABLED=false`
- `CASCADE_LIVE_DEMO_MODE=false`

Use port-forwarding for local access:

```powershell
kubectl port-forward -n cascade-system svc/command-center 18300:8030
```

Do not expose internal services such as ClickHouse, Redpanda, Qdrant, executors, or planner APIs directly to untrusted networks.

## Production-Safe Mode

For any shared environment, enable authentication on Command Center API and sensitive backend services:

```powershell
CASCADE_AUTH_ENABLED=true
CASCADE_LOCAL_DEMO_AUTH_BYPASS=false
CASCADE_API_KEYS=<operator-api-key>
CASCADE_AUTH_HEADER=Authorization
CASCADE_APPROVAL_SIGNING_SECRET=<random-signing-secret>
```

`CASCADE_API_KEY_HASHES` may be used instead of raw keys. Values are comma-separated SHA-256 hex digests of accepted API keys.

Sensitive write routes require auth when `CASCADE_AUTH_ENABLED=true`, including:

- remediation approval and execution
- rollback execution
- chaos run execution and cleanup
- chaos campaign start/pause/resume/stop
- Autopilot run start
- scheduler item updates, force-runs, and ticks
- state wipe/reset actions if exposed through a service

Health, readiness, and read-only dashboard queries remain available for local diagnostics.

## Binding Guard

Every FastAPI service main calls the shared `binding_guard` helper (`services/shared/security/auth.py`) at startup. It compares the effective bind host (explicit argument, then `CASCADE_BIND_HOST`, then `127.0.0.1`) against loopback addresses:

- non-local bind + auth disabled: always logs a CRITICAL warning.
- same condition with `CASCADE_STRICT_AUTH=true`: refuses boot with `RuntimeError`.

Kubernetes manifests set `CASCADE_BIND_HOST=0.0.0.0` so the guard reflects real in-cluster exposure; operators can flip `CASCADE_STRICT_AUTH=true` per namespace to fail closed until API-key auth is configured.

## Command Center Token

The Command Center frontend sends a bearer token when either is configured:

- `VITE_CASCADE_API_TOKEN`
- browser local storage key `cascade.apiToken`

Example browser console setup for a local authenticated session:

```javascript
localStorage.setItem("cascade.apiToken", "<operator-api-key>");
```

## Approval Binding

Approval records are tied to the exact plan they approve. Approval metadata includes:

- actor
- target action
- target service and namespace
- risk level
- policy decision
- plan hash
- nonce
- expiration
- optional HMAC signature when `CASCADE_APPROVAL_SIGNING_SECRET` is set

If the plan changes after approval, executors reject the approval. Expired, rejected, missing, unsigned-invalid, or mismatched approvals do not authorize live execution.

## Kubernetes RBAC

Cascade separates service accounts by responsibility:

- `observation-service`: read-only pods in `cascade-targets`
- `topology-service`: read-only services, pods, deployments, and replicasets in `cascade-targets`
- `chaos-executor-service`: reads pods/services and creates/deletes only supported Chaos Mesh resources in `cascade-targets`
- `remediation-executor-service`: reads pods/services, patches deployments/scale in `cascade-targets`, and cleans up Cascade-managed Chaos Mesh resources

Executors must not receive access to Secrets, ConfigMaps, RBAC resources, ServiceAccounts, or Namespaces. Tests enforce this least-privilege shape.

## Ingress And TLS

The repo does not ship a production ingress overlay. For production-style exposure:

- put Command Center behind an ingress, API gateway, or identity-aware proxy
- terminate TLS with a trusted certificate
- require authentication at the edge and in Cascade
- keep backend services cluster-internal
- add network policy so only Command Center API reaches internal HTTP services
- avoid exposing ClickHouse, Redpanda, Qdrant, executor services, or Kubernetes APIs

Local port-forwarding remains the recommended demo path.

## Secret Hygiene

Do not commit real keys, tokens, kubeconfigs, database dumps, support bundles, backups, or generated state. Run:

```powershell
.\scripts\audit-secrets.ps1
```

The audit script redacts values and fails on high-confidence secrets such as private keys, JWT-like tokens, kubeconfig credential data, and credential-bearing URLs.

## Known Limitations

- API-key auth is an MVP guard, not a replacement for SSO/OIDC and an API gateway.
- Rate limiting is in-memory and per-pod.
- Local ClickHouse/Redpanda/Qdrant defaults are not a hardened production data platform.
- Live chaos/remediation remains local/dev/staging only and must not be run against production workloads.
