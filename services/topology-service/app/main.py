from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from app.models import BlastRadiusRequest, CriticalPathsRequest, DependenciesResponse, DirectionalServicesResponse, HealthResponse, ImpactRequest, ImpactResponse, TrafficInferenceRequest, TopologyRefreshRequest, TopologyResponse
from app.topology import DEPENDENCIES, blast_radius, catalog_payload, critical_paths, dependencies, downstream, graph_payload, impact, upstream
from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.targets.catalog import ACTIVE_TARGET
from services.shared.topology.discovery import build_discovered_topology
from services.shared.security.auth import binding_guard

clickhouse = ClickHouseClient()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await clickhouse.initialize_schema()
    yield


app = FastAPI(title="Cascade Topology Service", version="0.1.0", lifespan=lifespan)

binding_guard()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="topology-service")


@app.get("/topology", response_model=TopologyResponse)
async def topology() -> TopologyResponse:
    return TopologyResponse(dependencies=DEPENDENCIES)


@app.get("/topology/graph")
async def topology_graph() -> dict:
    return await graph_payload()


@app.post("/topology/refresh")
async def topology_refresh(_payload: TopologyRefreshRequest | None = None) -> dict:
    payload = await graph_payload()
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            "topology.discovery.refreshed",
            "topology",
            payload=payload,
            correlation_id=str(payload.get("snapshot_id") or ""),
            namespace=ACTIVE_TARGET.namespace,
            action="refresh_topology",
            status=str(payload.get("discovery_status") or "refreshed"),
            topology_node_or_edge_id=str(payload.get("snapshot_id") or ""),
            evidence_summary=f"{len(payload.get('nodes', []))} nodes and {len(payload.get('edges', []))} edges discovered",
            user_safe_message="Topology discovery refreshed",
        ),
    )
    return payload


@app.get("/topology/evidence")
async def topology_evidence() -> dict:
    payload = await graph_payload()
    return {
        "snapshot_id": payload.get("snapshot_id"),
        "captured_at": payload.get("captured_at"),
        "discovery_status": payload.get("discovery_status"),
        "source_summary": payload.get("source_summary", {}),
        "traffic_summary": payload.get("traffic_summary", {}),
        "nodes": [
            {
                "id": node.get("id"),
                "kind": node.get("kind"),
                "namespace": node.get("namespace"),
                "source_type": node.get("source_type"),
                "confidence": node.get("confidence"),
                "evidence": node.get("evidence"),
                "last_seen": node.get("last_seen"),
            }
            for node in payload.get("nodes", [])
            if isinstance(node, dict)
        ],
        "edges": [
            {
                "id": edge.get("id"),
                "source": edge.get("source"),
                "target": edge.get("target"),
                "relation": edge.get("relation"),
                "source_type": edge.get("source_type"),
                "confidence": edge.get("confidence"),
                "evidence": edge.get("evidence"),
                "last_seen": edge.get("last_seen"),
                "metadata": edge.get("metadata") or {},
            }
            for edge in payload.get("edges", [])
            if isinstance(edge, dict)
        ],
        "limitations": payload.get("limitations", []),
        "warnings": payload.get("warnings", []),
    }


@app.post("/topology/traffic-inference")
async def topology_traffic_inference(payload: TrafficInferenceRequest) -> dict:
    """Analyze optional trace/traffic edge evidence without requiring a tracing backend."""
    events = []
    for edge in payload.edges:
        item = edge.model_dump(exclude_none=True)
        item["evidence_source"] = item.get("evidence_source") or payload.evidence_source
        events.append(item)
    source = payload.evidence_source.lower()
    return build_discovered_topology(
        ACTIVE_TARGET,
        trace_events=events if source in {"trace", "jaeger", "tempo", "opentelemetry"} else None,
        telemetry_events=events if source not in {"trace", "jaeger", "tempo", "opentelemetry"} else None,
        discovery_status="traffic_evidence_only",
        warnings=["Traffic inference endpoint analyzed supplied edge evidence only; it did not query a live tracing backend."],
    )


@app.get("/topology/catalog")
async def topology_catalog() -> dict:
    return catalog_payload()


@app.get("/target/workload")
async def target_workload() -> dict:
    return {
        "name": ACTIVE_TARGET.name,
        "namespace": ACTIVE_TARGET.namespace,
        "frontend_service": ACTIVE_TARGET.frontend_service,
        "services": list(ACTIVE_TARGET.services),
        "dependency_edges": [list(edge) for edge in ACTIVE_TARGET.dependency_edges],
        "dependencies": ACTIVE_TARGET.dependencies,
        "safe_chaos_services": list(ACTIVE_TARGET.safe_chaos_services),
        "protected_services": list(ACTIVE_TARGET.protected_services),
        "load_generator": {
            "name": ACTIVE_TARGET.load_generator.name,
            "image": ACTIVE_TARGET.load_generator.image,
            "target_url": ACTIVE_TARGET.load_generator.target_url,
            "users": ACTIVE_TARGET.load_generator.users,
            "spawn_rate": ACTIVE_TARGET.load_generator.spawn_rate,
        },
    }


@app.get("/topology/{service_name}/downstream", response_model=DirectionalServicesResponse)
async def downstream_services(service_name: str) -> DirectionalServicesResponse:
    return DirectionalServicesResponse(service_name=service_name, downstream=downstream(service_name))


@app.get("/topology/{service_name}/upstream", response_model=DirectionalServicesResponse)
async def upstream_services(service_name: str) -> DirectionalServicesResponse:
    return DirectionalServicesResponse(service_name=service_name, upstream=upstream(service_name))


@app.get("/topology/{service_name}/dependencies", response_model=DependenciesResponse)
async def service_dependencies(
    service_name: str,
    hops: int = Query(default=2, ge=0, le=12),
    direction: str = Query(default="downstream", pattern="^(upstream|downstream)$"),
) -> DependenciesResponse:
    return DependenciesResponse(service_name=service_name, direction=direction, hops=hops, dependencies=dependencies(service_name, hops=hops, direction=direction))


@app.post("/topology/impact", response_model=ImpactResponse)
async def topology_impact(payload: ImpactRequest) -> ImpactResponse:
    path, affected = impact(payload.root_service)
    return ImpactResponse(root_service=payload.root_service, impact_path=path, affected_services=affected)


@app.post("/topology/blast-radius")
async def topology_blast_radius(payload: BlastRadiusRequest) -> dict:
    return blast_radius(payload.root_service, hops=payload.hops, depth_cap=payload.depth_cap)


@app.post("/topology/critical-paths")
async def topology_critical_paths(payload: CriticalPathsRequest) -> dict:
    return critical_paths(payload.root_service, depth_cap=payload.depth_cap, limit=payload.limit)
