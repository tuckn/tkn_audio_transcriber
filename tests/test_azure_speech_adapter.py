from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from audio_transcriber.azure_speech_adapter import (
    AZURE_SPEECH_TOKEN_SCOPE,
    AzureSpeechFastAdapter,
)
from audio_transcriber.config import AzureSpeechTranscriptionConfig, resolve_config
from audio_transcriber.errors import AzureSpeechError, AzureSubmissionOutcomeUnknownError


class FakeToken:
    token = "CANARY_TOKEN_VALUE"


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.closed = False

    def get_token(self, *scopes: str, **kwargs: Any) -> FakeToken:
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
    adapter, _, client = make_adapter(
        tmp_path, lambda url, kwargs: next(responses), sleeps=sleeps
    )

    result = adapter.transcribe(audio)

    assert result.attempts == 2
    assert result.retries == 1
    assert len(client.calls) == 2
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


def test_credential_failure_is_safe_and_never_creates_http_client(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"audio")
    client_calls: list[str] = []

    class FailingCredential:
        def get_token(self, *scopes: str, **kwargs: Any) -> FakeToken:
            raise RuntimeError("CANARY_CREDENTIAL_DETAIL")

    adapter = AzureSpeechFastAdapter(
        config=make_config(tmp_path),
        logger=logging.getLogger("audio_transcriber.tests.azure-auth"),
        credential_factory=lambda: FailingCredential(),
        client_factory=lambda timeout: client_calls.append("created"),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(AzureSpeechError) as captured:
        adapter.transcribe(audio)

    assert str(captured.value) == "Azure Speech Entra authentication failed before submission"
    assert "CANARY_CREDENTIAL_DETAIL" not in str(captured.value)
    assert client_calls == []
