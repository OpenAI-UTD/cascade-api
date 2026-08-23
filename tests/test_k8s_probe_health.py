from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.test_k8s_resource_profiles import _base_deployments


def test_every_base_deployment_has_httget_liveness_and_readiness_probes() -> None:
    # Redpanda is a Kafka broker: its Kafka port is not HTTP, so it uses
    # tcp-socket probes by design (documented in docs/operations/runbook.md).
    tcp_socket_only = {"redpanda"}
    failures: list[str] = []
    for path, deployment in _base_deployments():
        name = deployment["metadata"]["name"]
        containers = deployment["spec"]["template"]["spec"].get("containers", [])
        for position, container in enumerate(containers):
            container_name = container.get("name") or f"container-{position}"
            for probe_name in ("livenessProbe", "readinessProbe"):
                label = f"{path}: {name}/{container_name} {probe_name}"
                probe = container.get(probe_name)
                if probe is None:
                    failures.append(f"{label}: missing")
                    continue
                if "httpGet" in probe:
                    http_get = probe["httpGet"]
                    if not isinstance(http_get, dict):
                        failures.append(f"{label}: httpGet must be a mapping")
                        continue
                    for field in ("path", "port"):
                        if field not in http_get:
                            failures.append(f"{label}: httpGet missing {field}")
                    continue
                if "tcpSocket" in probe and name in tcp_socket_only:
                    continue
                failures.append(f"{label}: expected httpGet health path/port")

    assert failures == []


def test_probe_health_paths_resolve_to_declared_container_ports() -> None:
    failures: list[str] = []
    for path, deployment in _base_deployments():
        name = deployment["metadata"]["name"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        declared_ports = {
            port.get("name"): port.get("containerPort")
            for port in container.get("ports", [])
            if port.get("name")
        }
        for probe_name in ("livenessProbe", "readinessProbe"):
            http_get = (container.get(probe_name) or {}).get("httpGet") or {}
            port = http_get.get("port")
            if port is None:
                continue  # tcp-socket-only workloads handled above
            if isinstance(port, str):
                if port not in declared_ports:
                    failures.append(f"{path}: {name} {probe_name} references undeclared port {port!r}")
            elif port not in declared_ports.values():
                failures.append(f"{path}: {name} {probe_name} references undeclared numeric port {port!r}")

    assert failures == []


def test_validator_script_enforces_probes_offline(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "validate-k8s-manifests.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Validated" in result.stdout

    broken = tmp_path / "broken-deployment.yaml"
    broken.write_text(
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: probeless
  namespace: cascade-system
spec:
  selector:
    matchLabels:
      app: probeless
  template:
    metadata:
      labels:
        app: probeless
    spec:
      containers:
        - name: probeless
          image: cascade-probeless:dev
          ports:
            - name: http
              containerPort: 9999
""",
        encoding="utf-8",
    )
    doc: dict[str, Any] = yaml.safe_load(broken.read_text(encoding="utf-8"))
    assert doc["kind"] == "Deployment"
    # Direct validator unit check on the synthetic document.
    sys.path.insert(0, str(script.parent))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("validate_k8s_manifests_mod", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        failures = module.validate_doc(Path("infra/kubernetes/broken/deployment.yaml"), 1, doc)
        assert any("livenessProbe" in failure and "missing probe" in failure for failure in failures)
    finally:
        sys.path.pop(0)
