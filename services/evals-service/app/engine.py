from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .schemas import Case, CriterionScore, RubricCriterion

TOKENS_PER_CHARACTER = 4
LENGTH_TARGET_WORDS = 30
KEYWORD_COVERAGE_POINTS = 3.0
LENGTH_ADEQUACY_POINTS = 1.0
EXPECTED_BONUS_POINTS = 1.0
MIN_KEYWORD_LENGTH = 3
DEFAULT_PRICE_IN_PER_MTOK = 0.15
DEFAULT_PRICE_OUT_PER_MTOK = 0.60
LLM_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class EngineSettings:
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    price_in_per_mtok: float = DEFAULT_PRICE_IN_PER_MTOK
    price_out_per_mtok: float = DEFAULT_PRICE_OUT_PER_MTOK

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url.strip() and self.llm_model.strip())


def estimate_tokens(text: str) -> int:
    return max(0, len(text) // TOKENS_PER_CHARACTER)


def estimate_cost(tokens_in: int, tokens_out: int, settings: EngineSettings) -> float:
    cost = (tokens_in / 1_000_000 * settings.price_in_per_mtok) + (
        tokens_out / 1_000_000 * settings.price_out_per_mtok
    )
    return round(cost, 6)


def heuristic_scores(case: Case, rubric: list[RubricCriterion]) -> dict[str, CriterionScore]:
    """Deterministically score one case output against the rubric.

    Formula per criterion (all components are deterministic, no network):

    - keyword coverage k: fraction of rubric-name tokens (lowercased
      alphanumeric words of length >= 3) that appear in the candidate text;
      0.0 when the name has no eligible tokens.
    - length adequacy l: min(1.0, word_count / LENGTH_TARGET_WORDS).
    - expected bonus e: 1.0 when ``case.expected`` is a non-empty
      case-insensitive substring of the candidate text, else 0.0.

    score = round(min(5.0, KEYWORD_COVERAGE_POINTS * k + LENGTH_ADEQUACY_POINTS * l
                      + EXPECTED_BONUS_POINTS * e), 2)

    With KEYWORD_COVERAGE_POINTS=3.0, LENGTH_ADEQUACY_POINTS=1.0,
    EXPECTED_BONUS_POINTS=1.0 the maximum without the expected bonus is 4.0.
    """
    candidate = case.input.casefold()
    words = len(case.input.split())
    length_adequacy = min(1.0, words / LENGTH_TARGET_WORDS)
    expected_bonus = _expected_bonus(case, candidate)
    scores: dict[str, CriterionScore] = {}
    for criterion in rubric:
        tokens = _criterion_keywords(criterion.name)
        matched = sum(1 for token in tokens if token in candidate)
        coverage = (matched / len(tokens)) if tokens else 0.0
        raw = (
            KEYWORD_COVERAGE_POINTS * coverage
            + LENGTH_ADEQUACY_POINTS * length_adequacy
            + EXPECTED_BONUS_POINTS * expected_bonus
        )
        score = round(min(5.0, raw), 2)
        scores[criterion.name] = CriterionScore(
            score=score,
            justification=(
                f"keyword coverage {coverage:.2f} ({matched}/{len(tokens) or 'n/a'}); "
                f"length {words} words (adequacy {length_adequacy:.2f}); "
                f"expected-substring bonus {'applied' if expected_bonus else 'not applied'}"
            ),
        )
    return scores


def llm_scores(
    case: Case,
    rubric: list[RubricCriterion],
    settings: EngineSettings,
) -> tuple[dict[str, CriterionScore], int, int]:
    """Ask an OpenAI-compatible chat-completions endpoint to judge one case.

    Sends the rubric and candidate text with JSON mode enabled and expects a
    JSON object of the shape::

        {"criteria": [{"name": "<criterion>", "score": 0-5, "justification": "..."}]}

    Returns per-criterion scores plus estimated prompt/completion tokens.
    Raises on any network or parsing failure so callers can fall back to the
    heuristic adapter and mark the result degraded.
    """
    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _judge_system_prompt()},
            {"role": "user", "content": _judge_user_prompt(case, rubric)},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key.strip():
        headers["Authorization"] = f"Bearer {settings.llm_api_key.strip()}"
    request = Request(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:  # noqa: S310 - operator-configured base URL
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    judged = json.loads(content)
    criteria = judged["criteria"]
    usage = body.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or estimate_tokens(_judge_user_prompt(case, rubric)))
    tokens_out = int(usage.get("completion_tokens") or estimate_tokens(content))
    by_name = {item.get("name"): item for item in criteria}
    scores: dict[str, CriterionScore] = {}
    for criterion in rubric:
        item = by_name.get(criterion.name)
        if not isinstance(item, dict):
            raise ValueError(f"judge response missing criterion: {criterion.name}")
        scores[criterion.name] = CriterionScore(
            score=max(0.0, min(5.0, float(item.get("score", 0.0)))),
            justification=str(item.get("justification", "")),
        )
    return scores, tokens_in, tokens_out


def run_case(
    case: Case,
    rubric: list[RubricCriterion],
    settings: EngineSettings,
) -> tuple[dict[str, CriterionScore], float, int, int, bool]:
    """Execute one case and return (scores, latency_ms, tokens_in, tokens_out, degraded).

    The openai-compatible adapter falls back to the deterministic heuristic
    scorer on any failure and marks the result degraded=true.
    """
    started = time.perf_counter()
    degraded = False
    if settings.llm_configured:
        try:
            scores, tokens_in, tokens_out = llm_scores(case, rubric, settings)
        except (HTTPError, URLError, TimeoutError, OSError, KeyError, TypeError, ValueError):
            scores = heuristic_scores(case, rubric)
            tokens_in = estimate_tokens(case.input)
            tokens_out = 0
            degraded = True
    else:
        scores = heuristic_scores(case, rubric)
        tokens_in = estimate_tokens(case.input)
        tokens_out = 0
    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    return scores, latency_ms, tokens_in, tokens_out, degraded


def weighted_score(scores: dict[str, CriterionScore], rubric: list[RubricCriterion]) -> float:
    total_weight = sum(max(0.0, min(1.0, criterion.weight)) for criterion in rubric)
    if total_weight <= 0:
        return 0.0
    weighted = sum(
        max(0.0, min(1.0, criterion.weight)) * scores[criterion.name].score
        for criterion in rubric
        if criterion.name in scores
    )
    return round(weighted / total_weight, 4)


def _expected_bonus(case: Case, candidate: str) -> float:
    expected = (case.expected or "").strip()
    if not expected:
        return 0.0
    return 1.0 if expected.casefold() in candidate else 0.0


def _criterion_keywords(name: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", name.casefold()) if len(token) >= MIN_KEYWORD_LENGTH]


def _judge_system_prompt() -> str:
    return (
        "You are a strict prompt-eval judge. Score the candidate output against every "
        "rubric criterion on a 0-5 scale. Respond only with a JSON object of the shape "
        '{"criteria": [{"name": "<criterion name>", "score": <0-5 number>, '
        '"justification": "<short reason>"}]} including every criterion exactly once.'
    )


def _judge_user_prompt(case: Case, rubric: list[RubricCriterion]) -> str:
    criteria_lines = "\n".join(
        f"- {criterion.name} (weight {criterion.weight}): {criterion.description}".rstrip()
        for criterion in rubric
    )
    expected_line = f"\nExpected reference answer: {case.expected}" if case.expected else ""
    return (
        f"Case: {case.name}\n\nRubric:\n{criteria_lines}\n\n"
        f"Candidate output to score:\n{case.input}{expected_line}"
    )


def adapters_catalog(settings: EngineSettings) -> list[dict[str, Any]]:
    return [
        {
            "name": "heuristic",
            "configured": True,
            "description": "Deterministic local scorer: rubric-name keyword coverage, length adequacy, expected-substring bonus. No network calls.",
        },
        {
            "name": "openai-compatible",
            "configured": settings.llm_configured,
            "description": "Judges outputs through an OpenAI-compatible /chat/completions endpoint in JSON mode; falls back to the heuristic adapter (degraded=true) on any failure.",
        },
    ]
