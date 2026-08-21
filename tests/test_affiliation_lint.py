from __future__ import annotations

from services.shared.qa.affiliation import affiliation_findings
from services.shared.qa.schemas import DocumentationInput, SourceFile


def test_forbidden_openai_identity() -> None:
    findings = affiliation_findings(
        [SourceFile(path="README.md", content="We are OpenAI and this is our club.")],
        [],
        evaluation_id="qa_test",
    )
    assert any("company identity" in finding["title"].lower() for finding in findings)


def test_missing_club_name_on_readme() -> None:
    findings = affiliation_findings(
        [SourceFile(path="README.md", content="# Student club\n\nA student organization at UTD.")],
        [],
        evaluation_id="qa_test",
    )
    assert any("missing club affiliation" in finding["title"].lower() for finding in findings)


def test_passes_with_club_copy() -> None:
    findings = affiliation_findings(
        [
            SourceFile(
                path="README.md",
                content="# OpenAI Club at UT Dallas\n\nMIT License\n\nOfficially affiliated student org.",
            )
        ],
        [],
        evaluation_id="qa_test",
    )
    forbidden = [finding for finding in findings if finding["severity"] in {"high", "critical"}]
    assert forbidden == []
