from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RunStatusName = Literal["queued", "running", "completed", "failed"]
AdapterName = Literal["heuristic", "openai-compatible"]

WEIGHT_SUM_TOLERANCE = 0.01


class RubricCriterion(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    weight: float = Field(ge=0.0, le=1.0)
    description: str = ""


class Case(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    input: str
    expected: str | None = None


class RunCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    rubric: list[RubricCriterion] = Field(min_length=1, max_length=8)
    cases: list[Case] = Field(min_length=1, max_length=50)
    adapter: AdapterName = "heuristic"
    model: str | None = None

    @model_validator(mode="after")
    def _validate_weights(self) -> RunCreate:
        total = sum(criterion.weight for criterion in self.rubric)
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"rubric weights must sum to 1.0 (tolerance +/-{WEIGHT_SUM_TOLERANCE}), got {total:.4f}"
            )
        return self


class CriterionScore(BaseModel):
    score: float = Field(ge=0.0, le=5.0)
    justification: str = ""


class CaseResult(BaseModel):
    case: Case
    scores: dict[str, CriterionScore]
    weighted_score: float = Field(ge=0.0, le=5.0)
    latency_ms: float = Field(ge=0.0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    degraded: bool = False


class RunResult(BaseModel):
    id: str = Field(pattern=r"^run_")
    project_name: str
    adapter: AdapterName = "heuristic"
    model: str | None = None
    status: RunStatusName
    aggregate_score: float | None = Field(default=None, ge=0.0, le=5.0)
    created_at: str
    finished_at: str | None = None
    results: list[CaseResult] = Field(default_factory=list)
    error: str = ""
