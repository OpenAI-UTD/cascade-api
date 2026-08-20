from __future__ import annotations

from services.shared.qa.engine import evaluate
from services.shared.qa.schemas import EvaluationRequest


def _payload(mode: str = "recommend") -> dict:
    return {
        "project": {"project_id": "payments-api", "name": "Payments API", "team": "checkout"},
        "revision": "abc123",
        "branch": "feature/retry-policy",
        "mode": mode,
        "pipeline": {"provider": "github-actions", "pipeline_id": "991"},
        "checks": [
            {"name": "unit tests", "status": "failed", "kind": "test", "output": "RetryPolicyTest: timeout must be 30 seconds", "file": "tests/test_retry.py", "line": 42},
            {"name": "lint", "status": "passed", "kind": "lint"},
        ],
        "runtime_observations": [
            {"name": "checkout smoke test", "status": "degraded", "message": "upstream request timeout", "metric": "p95_ms", "expected": "<500", "actual": "2800"}
        ],
        "documentation": [
            {"path": "docs/retries.md", "content": "Payment retry timeout must be 30 seconds. Update RETRY_TIMEOUT_SECONDS when timeout tests fail.", "tags": ["retry", "timeout"]}
        ],
        "documented_fixes": [
            {
                "rule_id": "retry-timeout",
                "issue_patterns": ["timeout must be 30 seconds"],
                "title": "Use the documented retry timeout",
                "recommendation": "Set RETRY_TIMEOUT_SECONDS to 30 as required by docs/retries.md.",
                "documentation_path": "docs/retries.md",
                "changes": [{"path": "src/config.py", "original": "RETRY_TIMEOUT_SECONDS = 10", "replacement": "RETRY_TIMEOUT_SECONDS = 30"}],
            }
        ],
        "source_files": [{"path": "src/config.py", "content": "RETRY_TIMEOUT_SECONDS = 10\n"}],
        "fail_on": "high",
    }


def test_recommend_mode_normalizes_ci_and_runtime_findings() -> None:
    result = evaluate(EvaluationRequest.model_validate(_payload()))

    assert result.status == "issues_found"
    assert len(result.findings) == 2
    assert result.findings[0].matched_rule_id == "retry-timeout"
    assert result.findings[0].documentation_refs == ["docs/retries.md"]
    assert result.proposed_changes == []
    assert result.quality_gate.passed is False
    assert result.quality_gate.blocking_findings == 2


def test_propose_fix_only_returns_documented_applicable_change() -> None:
    result = evaluate(EvaluationRequest.model_validate(_payload("propose_fix")))

    assert result.status == "fixes_proposed"
    assert len(result.proposed_changes) == 1
    change = result.proposed_changes[0]
    assert change.path == "src/config.py"
    assert change.documentation_path == "docs/retries.md"
    assert change.replacement == "RETRY_TIMEOUT_SECONDS = 30"
    assert any(item.reason == "No documented fix rule matched this issue." for item in result.withheld_fixes)


def test_fix_is_withheld_when_source_does_not_match_exactly_once() -> None:
    payload = _payload("propose_fix")
    payload["source_files"][0]["content"] = "RETRY_TIMEOUT_SECONDS = 20\n"
    result = evaluate(EvaluationRequest.model_validate(payload))

    assert result.proposed_changes == []
    assert any("found 0" in item.reason for item in result.withheld_fixes)


def test_passing_evidence_passes_quality_gate() -> None:
    payload = _payload()
    payload["checks"] = [{"name": "unit tests", "status": "passed", "kind": "test"}]
    payload["runtime_observations"] = [{"name": "smoke", "status": "healthy"}]
    result = evaluate(EvaluationRequest.model_validate(payload))

    assert result.status == "passed"
    assert result.findings == []
    assert result.quality_gate.passed is True
