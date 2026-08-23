from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from services.shared.security.auth import AuthSettings, BindingGuardReport, binding_guard


@dataclass
class LegacySettings:
    cascade_auth_enabled: bool = False


def _run(settings=None, **kwargs) -> BindingGuardReport:
    kwargs.setdefault("logger", logging.getLogger("test.binding"))
    return binding_guard(settings, **kwargs)


def test_local_binding_with_auth_disabled_is_allowed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    with caplog.at_level(logging.CRITICAL):
        report = _run(host="127.0.0.1")
    assert report.local_binding is True
    assert report.exposed_unauthenticated is False
    assert report.refused is False
    assert not any(record.levelno >= logging.CRITICAL for record in caplog.records)


def test_loopback_variants_count_as_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    for host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        report = _run(host=host)
        assert report.local_binding is True


def test_nonlocal_binding_without_auth_logs_critical(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    with caplog.at_level(logging.CRITICAL):
        report = _run(host="0.0.0.0")
    assert report.local_binding is False
    assert report.auth_enabled is False
    assert report.exposed_unauthenticated is True
    assert report.refused is False
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_nonlocal_binding_with_auth_enabled_does_not_log_critical(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    with caplog.at_level(logging.CRITICAL):
        report = _run(AuthSettings(enabled=True), host="0.0.0.0")
    assert report.auth_enabled is True
    assert report.exposed_unauthenticated is False
    assert not any(record.levelno >= logging.CRITICAL for record in caplog.records)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
def test_strict_auth_refuses_exposed_unauthenticated_boot(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CASCADE_STRICT_AUTH", value)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        _run(host="0.0.0.0")


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "maybe"])
def test_strict_auth_non_truthy_values_only_warn(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str) -> None:
    monkeypatch.setenv("CASCADE_STRICT_AUTH", value)
    with caplog.at_level(logging.CRITICAL):
        report = _run(host="0.0.0.0")
    assert report.strict_auth is False
    assert report.refused is False


def test_strict_auth_allows_local_binding_and_authenticated_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASCADE_STRICT_AUTH", "true")
    assert _run(host="127.0.0.1").refused is False
    assert _run(LegacySettings(cascade_auth_enabled=True), host="0.0.0.0").refused is False


def test_bind_host_env_used_when_host_param_omitted(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("CASCADE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    with caplog.at_level(logging.CRITICAL):
        report = _run()
    assert report.host == "0.0.0.0"
    assert report.exposed_unauthenticated is True
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_explicit_host_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASCADE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    report = _run(host="localhost")
    assert report.host == "localhost"
    assert report.local_binding is True


def test_settings_variants_for_auth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASCADE_STRICT_AUTH", raising=False)
    assert _run(None, host="0.0.0.0").auth_enabled is False
    assert _run(LegacySettings(cascade_auth_enabled=False), host="0.0.0.0").auth_enabled is False
    assert _run(LegacySettings(cascade_auth_enabled=True), host="0.0.0.0").auth_enabled is True
    assert _run(AuthSettings(enabled=False), host="0.0.0.0").auth_enabled is False
