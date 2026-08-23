from __future__ import annotations

from fastapi import FastAPI

from app.models import HealthResponse, IncidentReport, ReportRequest, TimelineRequest, TimelineResponse
from app.report import build_markdown
from app.timeline import build_timeline
from services.shared.security.auth import binding_guard

app = FastAPI(title="Cascade Incident Timeline Service", version="0.1.0")

binding_guard()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="incident-timeline-service")


@app.post("/timeline", response_model=TimelineResponse)
async def timeline(payload: TimelineRequest) -> TimelineResponse:
    return TimelineResponse(events=build_timeline(payload.experiment, payload.incident, payload.topology_impact))


@app.post("/report", response_model=IncidentReport)
async def report(payload: ReportRequest) -> IncidentReport:
    incident = payload.incident
    root = incident.get("root_cause_service")
    chain = incident.get("causal_chain", []) or []
    affected = incident.get("affected_services", []) or []
    evidence = incident.get("evidence", []) or []
    summary = f"Incident linked to experiment {incident.get('experiment_id')} with root candidate {root or 'unknown'}."
    steps = [
        "Review pod restarts and Kubernetes events for the root candidate.",
        "Compare Prometheus telemetry before and after the experiment window.",
        "Inspect upstream callers and downstream dependencies from the topology impact path.",
    ]
    markdown = build_markdown(summary, root, chain, affected, evidence, steps)
    return IncidentReport(
        summary=summary,
        root_cause=root,
        causal_chain=chain,
        affected_services=affected,
        evidence=evidence,
        recommended_next_steps=steps,
        markdown=markdown,
    )
