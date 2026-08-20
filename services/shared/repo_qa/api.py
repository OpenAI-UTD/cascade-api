from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class QaApiError(RuntimeError):
    pass


class QaClient(ABC):
    @abstractmethod
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class HttpQaClient(QaClient):
    def __init__(self, base_url: str, *, api_key: str = "", timeout_seconds: int = 30) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise QaApiError("QA API URL must use http:// or https://")
        self.endpoint = base_url.rstrip("/") if base_url.rstrip("/").endswith("/evaluations") else base_url.rstrip("/") + "/evaluations"
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "cascade-repo-qa-runner/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - URL scheme validated in __init__
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-4000:]
            raise QaApiError(f"QA API returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise QaApiError(f"QA API request failed: {exc.__class__.__name__}: {exc}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("quality_gate"), dict):
            raise QaApiError("QA API returned an invalid evaluation response")
        return result


class InProcessQaClient(QaClient):
    """Test client that evaluates the same API contract without an HTTP server."""

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        from services.shared.qa import EvaluationRequest, evaluate

        return evaluate(EvaluationRequest.model_validate(payload)).model_dump(mode="json")
