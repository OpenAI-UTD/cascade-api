from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["info", "low", "medium", "high", "critical"]


class ProjectRef(BaseModel):
    project_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=200)
    repository: str = Field(default="", max_length=500)
    team: str = Field(default="", max_length=200)


class PipelineRef(BaseModel):
    provider: str = Field(default="generic", max_length=80)
    pipeline_id: str = Field(default="", max_length=200)
    job_url: str = Field(default="", max_length=1000)


class CheckResult(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    status: Literal["passed", "failed", "error", "skipped"]
    kind: Literal["test", "lint", "build", "security", "runtime", "other"] = "other"
    output: str = Field(default="", max_length=20_000)
    file: str = Field(default="", max_length=1000)
    line: int | None = Field(default=None, ge=1)


class RuntimeObservation(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    status: Literal["healthy", "degraded", "failed"]
    message: str = Field(default="", max_length=20_000)
    metric: str = Field(default="", max_length=200)
    expected: str = Field(default="", max_length=500)
    actual: str = Field(default="", max_length=500)
    file: str = Field(default="", max_length=1000)
    line: int | None = Field(default=None, ge=1)


class DocumentationInput(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=30)


class SourceFile(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=200_000)


class DocumentedChange(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    original: str = Field(min_length=1, max_length=50_000)
    replacement: str = Field(max_length=50_000)


class DocumentedFixRule(BaseModel):
    rule_id: str = Field(min_length=1, max_length=120)
    issue_patterns: list[str] = Field(min_length=1, max_length=30)
    title: str = Field(min_length=1, max_length=300)
    recommendation: str = Field(min_length=1, max_length=4000)
    documentation_path: str = Field(min_length=1, max_length=1000)
    changes: list[DocumentedChange] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def normalize_patterns(self) -> DocumentedFixRule:
        if not any(pattern.strip() for pattern in self.issue_patterns):
            raise ValueError("issue_patterns must contain at least one non-empty pattern")
        return self


class EvaluationRequest(BaseModel):
    project: ProjectRef
    revision: str = Field(default="", max_length=200)
    branch: str = Field(default="", max_length=200)
    mode: Literal["recommend", "propose_fix"] = "recommend"
    pipeline: PipelineRef = Field(default_factory=PipelineRef)
    checks: list[CheckResult] = Field(default_factory=list, max_length=500)
    runtime_observations: list[RuntimeObservation] = Field(default_factory=list, max_length=500)
    documentation: list[DocumentationInput] = Field(default_factory=list, max_length=100)
    documented_fixes: list[DocumentedFixRule] = Field(default_factory=list, max_length=100)
    source_files: list[SourceFile] = Field(default_factory=list, max_length=100)
    fail_on: Severity = "high"

    @model_validator(mode="after")
    def require_evidence(self) -> EvaluationRequest:
        if not self.checks and not self.runtime_observations:
            raise ValueError("at least one CI check or runtime observation is required")
        return self


class EvidenceRef(BaseModel):
    source: Literal["ci", "runtime", "documentation"]
    name: str
    path: str = ""
    line: int | None = None


class Finding(BaseModel):
    finding_id: str
    title: str
    severity: Severity
    category: str
    summary: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    documentation_refs: list[str] = Field(default_factory=list)
    recommendation: str
    matched_rule_id: str = ""


class ProposedChange(BaseModel):
    rule_id: str
    finding_id: str
    path: str
    original: str
    replacement: str
    documentation_path: str
    rationale: str


class WithheldFix(BaseModel):
    rule_id: str
    finding_id: str
    reason: str


class QualityGate(BaseModel):
    passed: bool
    fail_on: Severity
    blocking_findings: int
    reason: str


class EvaluationResult(BaseModel):
    evaluation_id: str
    created_at: str
    project: ProjectRef
    revision: str
    branch: str
    mode: Literal["recommend", "propose_fix"]
    status: Literal["passed", "issues_found", "fixes_proposed"]
    summary: str
    findings: list[Finding]
    proposed_changes: list[ProposedChange]
    withheld_fixes: list[WithheldFix]
    quality_gate: QualityGate
    stats: dict[str, int]
