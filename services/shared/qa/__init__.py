"""Project-agnostic quality assurance contracts and evaluation logic."""

from .engine import evaluate
from .schemas import EvaluationRequest, EvaluationResult

__all__ = ["EvaluationRequest", "EvaluationResult", "evaluate"]
