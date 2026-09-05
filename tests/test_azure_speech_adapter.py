from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest

from audio_transcriber.azure_speech_adapter import (
    AZURE_SPEECH_TOKEN_SCOPE,
    AzureSpeechFastAdapter,
    _default_credential_factory,
)
from audio_transcriber.config import AzureSpeechTranscriptionConfig, resolve_config
from audio_transcriber.errors import AzureSpeechError, AzureSubmissionOutcomeUnknownError
from audio_transcriber.logging_config import configure_logging


class FakeToken:
    token = "CANARY_TOKEN_VALUE"


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.authentication_scopes: list[list[str]] = []
        self.closed = False

    def authenticate(self, *, scopes: Iterable[str]) -> object:
        assert self.scopes == []
        self.authentication_scopes.append(list(scopes))
        return object()

    def get_token(self, *scopes: str, **kwargs: Any) -> FakeToken:
        assert self.authentication_scopes == [list(scopes)]
        self.scopes.extend(scopes)
        return FakeToken()

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, handler: Callable[[str, dict[str, Any]], httpx.Response]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)

    def close(self) -> None:
        self.closed = True


def make_config(tmp_path: Path, *, retries: int = 2) -> AzureSpeechTranscriptionConfig:
    resolved = resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        profile="cloud/azure-ja",
        cli_overrides={
            "azure_speech_endpoint": "https://example.cognitiveservices.azure.com/",
            "azure_speech_max_retries": retries,
        },
    )
    assert isinstance(resolved.transcription, AzureSpeechTranscriptionConfig)
    return resolved.transcription


def make_adapter(
    tmp_path: Path,
    handler: Callable[[str, dict[str, Any]], httpx.Response],
    *,
    retries: int = 2,
    sleeps: list[float] | None = None,
) -> tuple[AzureSpeechFastAdapter, FakeCredential, FakeClient]:
    credential = FakeCredential()
    client = FakeClient(handler)
    adapter = AzureSpeechFastAdapter(
        config=make_config(tmp_path, retries=retries),
        logger=logging.getLogger("audio_transcriber.tests.azure"),
        credential_factory=lambda: credential,
        client_factory=lambda timeout: client,
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda low, high: 0.0,
    )
    return adapter, credential, client


def test_success_uses_entra_multipart_and_preserves_speaker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    audio = tmp_path / "normalized.wav"
    audio_canary = "CANARY_SUCCESS_AUDIO_PAYLOAD"
    transcript_canary = "CANARY_SUCCESS_TRANSCRIPT"
    audio.write_bytes(audio_canary.encode())
    caplog.set_level(logging.INFO)

    def success(url: str, kwargs: dict[str, Any]) -> httpx.Response:
        assert credential.authentication_scopes == [[AZURE_SPEECH_TOKEN_SCOPE]]
        assert url.endswith("/speechtotext/transcriptions:transcribe")
        assert kwargs["params"] == {"api-version": "2025-10-15"}
        assert kwargs["headers"] == {"Authorization": "Bearer CANARY_TOKEN_VALUE"}
        assert kwargs["files"]["audio"][0] == "normalized_16k_mono.flac"
        assert kwargs["files"]["audio"][2] == "audio/flac"
        assert kwargs["files"]["audio"][1].read() == audio_canary.encode()
        assert '"locales":["ja-JP"]' in kwargs["data"]["definition"]
        assert '"diarization":{"enabled":true,"maxSpeakers":8}' in kwargs["data"][
            "definition"
        ]
        return httpx.Response(
            200,
            headers={"apim-request-id": "request-123"},
            json={
                "phrases": [
                    {
                        "offsetMilliseconds": 100,
                        "durationMilliseconds": 900,
                        "text": transcript_canary,
                        "speaker": 2,
                    }
                ]
            },
        )

    adapter, credential, client = make_adapter(tmp_path, success)
    result = adapter.transcribe(audio)

    assert credential.scopes == [AZURE_SPEECH_TOKEN_SCOPE]
    assert credential.authentication_scopes == [[AZURE_SPEECH_TOKEN_SCOPE]]
    assert credential.closed and client.closed
    assert result.attempts == 1
    assert result.request_id == "request-123"
    assert result.segments[0].speaker == "2"
    assert result.segments[0].chunk == "azure-whole-file"
    assert transcript_canary not in caplog.text
    assert audio_canary not in caplog.text
    assert FakeToken.token not in caplog.text
    assert "Authorization" not in caplog.text


def test_429_respects_retry_after_and_retries(tmp_path: Path) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio")
    responses = iter(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "0", "x-ms-request-id": "retry-request"},
                json={"error": {"code": "TooManyRequests", "message": "private"}},
            ),
            httpx.Response(200, json={"phrases": []}),
        ]
    )
    sleeps: list[float] = []
    adapter, credential, client = make_adapter(
        tmp_path, lambda url, kwargs: next(responses), sleeps=sleeps
    )

    result = adapter.transcribe(audio)

    assert result.attempts == 2
    assert result.retries == 1
    assert len(client.calls) == 2
    assert credential.authentication_scopes == [[AZURE_SPEECH_TOKEN_SCOPE]]
    assert credential.scopes == [AZURE_SPEECH_TOKEN_SCOPE]
    assert sleeps == [0.0]


def test_retryable_5xx_stops_at_configured_retry_cap(tmp_path: Path) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio")
    sleeps: list[float] = []
    adapter, _, client = make_adapter(
        tmp_path,
        lambda url, kwargs: httpx.Response(
            503,
            headers={"x-ms-request-id": "service-request"},
            json={"error": {"code": "ServiceUnavailable"}},
        ),
        retries=2,
        sleeps=sleeps,
    )

    with pytest.raises(AzureSpeechError, match="attempts=3"):
        adapter.transcribe(audio)

    assert len(client.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_pre_upload_connection_failure_is_retried(tmp_path: Path) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio")
    attempt = 0

    def handler(url: str, kwargs: dict[str, Any]) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise httpx.ConnectError(
                "connect failed", request=httpx.Request("POST", url)
            )
        return httpx.Response(200, json={"phrases": []})

    sleeps: list[float] = []
    adapter, _, client = make_adapter(tmp_path, handler, sleeps=sleeps)

    result = adapter.transcribe(audio)

    assert result.attempts == 2
    assert len(client.calls) == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 413])
def test_non_retryable_status_is_safe_and_not_retried(
    tmp_path: Path, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    audio = tmp_path / "normalized.wav"
    audio_canary = "CANARY_AUDIO_PAYLOAD"
    audio.write_bytes(audio_canary.encode())
    canary = "CANARY_PRIVATE_TRANSCRIPT_BODY"
    adapter, _, client = make_adapter(
        tmp_path,
        lambda url, kwargs: httpx.Response(
            status,
            headers={"x-ms-request-id": "safe-request-id"},
            json={"error": {"code": "SafeCode", "message": canary}},
        ),
    )

    with pytest.raises(AzureSpeechError) as captured:
        adapter.transcribe(audio)

    assert len(client.calls) == 1
    combined = str(captured.value) + caplog.text
    assert "SafeCode" in combined
    assert "safe-request-id" in combined
    assert canary not in combined
    assert audio_canary not in combined
    assert FakeToken.token not in combined
    assert "Authorization" not in combined


def test_confirmed_incomplete_upload_is_retried(tmp_path: Path) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio payload")
    attempt = 0

    def handler(url: str, kwargs: dict[str, Any]) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            stream = kwargs["files"]["audio"][1]
            stream.read(1)
            raise httpx.WriteTimeout("write timed out", request=httpx.Request("POST", url))
        return httpx.Response(200, json={"phrases": []})

    sleeps: list[float] = []
    adapter, _, client = make_adapter(tmp_path, handler, sleeps=sleeps)

    result = adapter.transcribe(audio)

    assert result.attempts == 2
    assert len(client.calls) == 2
    assert sleeps == [1.0]


def test_response_loss_after_upload_is_not_retried(tmp_path: Path) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio payload")

    def handler(url: str, kwargs: dict[str, Any]) -> httpx.Response:
        stream = kwargs["files"]["audio"][1]
        stream.read()
        raise httpx.ReadTimeout("response timed out", request=httpx.Request("POST", url))

    adapter, _, client = make_adapter(tmp_path, handler)

    with pytest.raises(AzureSubmissionOutcomeUnknownError, match="submission_outcome_unknown"):
        adapter.transcribe(audio)

    assert len(client.calls) == 1


@pytest.mark.parametrize("phase", ["authenticate", "get_token"])
def test_credential_failure_is_safe_and_never_creates_http_client(
    tmp_path: Path,
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio")
    client_calls: list[str] = []

    class FailingCredential(FakeCredential):
        def authenticate(self, *, scopes: Iterable[str]) -> object:
            if phase == "authenticate":
                raise RuntimeError("CANARY_CREDENTIAL_DETAIL")
            return super().authenticate(scopes=scopes)

        def get_token(self, *scopes: str, **kwargs: Any) -> FakeToken:
            raise RuntimeError("CANARY_CREDENTIAL_DETAIL")

    credential = FailingCredential()
    adapter = AzureSpeechFastAdapter(
        config=make_config(tmp_path),
        logger=logging.getLogger("audio_transcriber.tests.azure-auth"),
        credential_factory=lambda: credential,
        client_factory=lambda timeout: client_calls.append("created"),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(AzureSpeechError) as captured:
        adapter.transcribe(audio)

    assert "browser authentication failed or timed out before submission" in str(captured.value)
    assert "CANARY_CREDENTIAL_DETAIL" not in str(captured.value) + caplog.text
    assert credential.closed
    assert client_calls == []


def test_browser_authentication_interruption_closes_credential_without_upload(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "upload.flac"
    audio.write_bytes(b"audio")
    client_calls: list[str] = []

    class InterruptedCredential(FakeCredential):
        def authenticate(self, *, scopes: Iterable[str]) -> object:
            raise KeyboardInterrupt

    credential = InterruptedCredential()
    adapter = AzureSpeechFastAdapter(
        config=make_config(tmp_path),
        logger=logging.getLogger("audio_transcriber.tests.azure-auth"),
        credential_factory=lambda: credential,
        client_factory=lambda timeout: client_calls.append("created"),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(KeyboardInterrupt):
        adapter.transcribe(audio)

    assert credential.closed
    assert credential.scopes == []
    assert client_calls == []


def test_default_factory_uses_browser_only_without_persistent_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_options: list[dict[str, Any]] = []
    credentials: list[FakeCredential] = []

    def browser_factory(**kwargs: Any) -> FakeCredential:
        constructor_options.append(kwargs)
        credential = FakeCredential()
        credentials.append(credential)
        return credential

    def forbidden_default_chain(**kwargs: Any) -> None:
        pytest.fail("DefaultAzureCredential must never be used")

    monkeypatch.setattr("azure.identity.InteractiveBrowserCredential", browser_factory)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", forbidden_default_chain)
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "AzureCliCredential")
    monkeypatch.setenv("AZURE_CLIENT_ID", "CANARY_AMBIENT_CLIENT")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "CANARY_AMBIENT_SECRET")
    monkeypatch.setenv("AZURE_TENANT_ID", "CANARY_AMBIENT_TENANT")

    first = _default_credential_factory()
    second = _default_credential_factory()

    assert first is credentials[0]
    assert second is credentials[1]
    assert first is not second
    assert constructor_options == [
        {
            "disable_automatic_authentication": True,
            "cache_persistence_options": None,
            "timeout": 300,
        }
    ] * 2


@pytest.mark.parametrize("outcome", ["success", "browser_unavailable", "timeout", "denied"])
def test_real_sdk_browser_flow_requests_account_selection_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Keep the real SDK's public authenticate/get_token and account-selection flow.
    # Replace only its browser, callback server, and MSAL network/cache boundary.
    from azure.identity import InteractiveBrowserCredential
    from azure.identity._credentials import browser

    for namespace in ("audio_transcriber", "azure.identity", "azure.core", "msal"):
        test_logger = logging.getLogger(namespace)
        monkeypatch.setattr(test_logger, "handlers", [])
        monkeypatch.setattr(test_logger, "propagate", True)
        monkeypatch.setattr(test_logger, "level", logging.NOTSET)
    log_output = io.StringIO()
    logger = configure_logging(quiet=False, verbose=True, stream=log_output)
    audio = tmp_path / "upload.flac"
    audio.write_bytes(b"audio")
    flow_options: list[dict[str, Any]] = []
    browser_urls: list[str] = []
    events: list[str] = []
    server_options: list[tuple[str, int, int]] = []
    auth_result = {
        "access_token": FakeToken.token,
        "expires_in": 3600,
        "id_token_claims": {
            "iss": "https://login.microsoftonline.com/example-tenant/v2.0",
            "tid": "example-tenant",
            "aud": "example-client",
            "sub": "example-account",
            "preferred_username": "CANARY_USER@example.invalid",
        },
    }

    class FakeRedirectServer:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            server_options.append((host, port, timeout))

        def wait_for_redirect(self) -> dict[str, str] | None:
            events.append("callback")
            return None if outcome == "timeout" else {"code": "example-code"}

    class FakeMsalApp:
        def initiate_auth_code_flow(self, scopes: list[str], **kwargs: Any) -> dict[str, str]:
            assert scopes == [AZURE_SPEECH_TOKEN_SCOPE]
            flow_options.append(kwargs)
            return {"auth_uri": "https://login.microsoftonline.com/example-authorize"}

        def acquire_token_by_auth_code_flow(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            events.append("authenticate")
            if outcome == "denied":
                return {
                    "error": "access_denied",
                    "error_description": "CANARY_PRIVATE_AUTH_RESPONSE",
                }
            return auth_result

        def get_accounts(self, *, username: str) -> list[dict[str, str]]:
            assert username == "CANARY_USER@example.invalid"
            return [{"home_account_id": "example-account"}]

        def acquire_token_silent_with_error(
            self, scopes: list[str], **kwargs: Any
        ) -> dict[str, Any]:
            events.append("get_token")
            return auth_result

    def open_browser(url: str) -> bool:
        browser_urls.append(url)
        events.append("browser")
        return outcome != "browser_unavailable"

    def submit(url: str, kwargs: dict[str, Any]) -> httpx.Response:
        events.append("upload")
        assert kwargs["headers"]["Authorization"] == f"Bearer {FakeToken.token}"
        return httpx.Response(200, json={"phrases": []})

    client = FakeClient(submit)
    monkeypatch.setattr(browser, "AuthCodeRedirectServer", FakeRedirectServer)
    monkeypatch.setattr(browser, "_open_browser", open_browser)
    monkeypatch.setattr(InteractiveBrowserCredential, "_get_app", lambda *a, **kw: FakeMsalApp())
    adapter = AzureSpeechFastAdapter(
        config=make_config(tmp_path),
        logger=logger,
        client_factory=lambda timeout: client,
    )

    if outcome == "success":
        # Even reusing the adapter must not silently reuse the last chosen account.
        adapter.transcribe(audio)
        adapter.transcribe(audio)
        assert events == ["browser", "callback", "authenticate", "get_token", "upload"] * 2
    else:
        with pytest.raises(AzureSpeechError, match="browser authentication failed"):
            adapter.transcribe(audio)
        assert client.calls == []
        assert "get_token" not in events

    expected_runs = 2 if outcome == "success" else 1
    assert len(browser_urls) == expected_runs
    assert len(flow_options) == expected_runs
    assert server_options == [("localhost", 8400, 300)] * expected_runs
    for options in flow_options:
        assert options["prompt"] == "select_account"
        assert options["login_hint"] is None
    combined_logs = caplog.text + log_output.getvalue()
    assert FakeToken.token not in combined_logs
    assert "CANARY_USER" not in combined_logs
    assert "CANARY_PRIVATE_AUTH_RESPONSE" not in combined_logs
