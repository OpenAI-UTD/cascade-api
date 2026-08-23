from __future__ import annotations

from pathlib import Path
import sys

import yaml


KUSTOMIZE_API_GROUP = "kustomize.config.k8s.io/"

KNOWN_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "ConfigMap",
    "Deployment",
    "Job",
    "Namespace",
    "NetworkPolicy",
    "PersistentVolumeClaim",
    "Role",
    "RoleBinding",
    "Service",
    "ServiceAccount",
}

SPEC_REQUIRED_KINDS = {
    "Deployment",
    "Job",
    "NetworkPolicy",
    "PersistentVolumeClaim",
    "Service",
}

CRD_TEMPLATE_PREFIX = ("infra", "kubernetes", "chaos-templates")

# Infra workloads whose health cannot be checked over HTTP use tcp-socket
# probes instead; everything else must expose httpGet liveness/readiness.
TCP_SOCKET_PROBE_ONLY = {"redpanda"}


def _validate_deployment_probes(path: Path, index: int, doc: dict) -> list[str]:
    failures: list[str] = []
    name = doc.get("metadata", {}).get("name") if isinstance(doc.get("metadata"), dict) else None
    containers = (
        doc.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    for position, container in enumerate(containers):
        container_name = container.get("name") or f"container-{position}"
        for probe_name in ("livenessProbe", "readinessProbe"):
            probe = container.get(probe_name)
            label = f"{path} document {index}: {name}/{container_name} {probe_name}"
            if probe is None:
                failures.append(f"{label}: missing probe")
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
            if "tcpSocket" in probe and name in TCP_SOCKET_PROBE_ONLY:
                continue
            failures.append(
                f"{label}: expected httpGet health path/port"
                + (" (tcp-socket exempt list applies)" if "tcpSocket" in probe else "")
            )
    return failures


def validate_doc(path: Path, index: int, doc: object) -> list[str]:
    failures: list[str] = []
    if doc is None:
        return failures
    if not isinstance(doc, dict):
        return [f"{path} document {index}: expected mapping"]

    api_version = doc.get("apiVersion")
    kind = doc.get("kind")
    if "apiVersion" not in doc:
        failures.append(f"{path} document {index}: missing apiVersion")
    if "kind" not in doc:
        failures.append(f"{path} document {index}: missing kind")

    if kind == "Kustomization":
        if not isinstance(api_version, str) or not api_version.startswith(KUSTOMIZE_API_GROUP):
            failures.append(f"{path} document {index}: unexpected Kustomization apiVersion {api_version!r}")
        if "resources" not in doc and "patches" not in doc and "components" not in doc:
            failures.append(f"{path} document {index}: Kustomization has no resources, patches, or components")
        return failures

    if "metadata" not in doc:
        failures.append(f"{path} document {index}: missing metadata")
    metadata = doc.get("metadata")
    if isinstance(metadata, dict) and not metadata.get("name"):
        failures.append(f"{path} document {index}: missing metadata.name")
    if kind in SPEC_REQUIRED_KINDS and "spec" not in doc:
        failures.append(f"{path} document {index}: missing spec")
    if kind not in KNOWN_KINDS and path.parts[:3] != CRD_TEMPLATE_PREFIX:
        failures.append(f"{path} document {index}: unexpected kind {kind!r}")
    if kind == "NetworkPolicy" and api_version != "networking.k8s.io/v1":
        failures.append(f"{path} document {index}: unexpected NetworkPolicy apiVersion {api_version!r}")
    if kind == "Deployment" and path.parts[:3] != CRD_TEMPLATE_PREFIX and "sock-shop" not in path.parts:
        failures.extend(_validate_deployment_probes(path, index, doc))

    return failures


def main() -> int:
    roots = [Path("infra/kubernetes"), Path("targets/sock-shop")]
    paths = sorted(path for root in roots for path in root.glob("**/*.yaml"))
    if not paths:
        print("No Kubernetes YAML files found", file=sys.stderr)
        return 1

    failures: list[str] = []
    for path in paths:
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except Exception as exc:
            failures.append(f"{path}: YAML parse failed: {exc}")
            continue
        for index, doc in enumerate(docs, start=1):
            failures.extend(validate_doc(path, index, doc))

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(f"Validated {len(paths)} Kubernetes YAML files offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
