from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from .config import AzureSpeechTranscriptionConfig
from .errors import AzureSpeechError, AzureSubmissionOutcomeUnknownError
from .models import Segment

AZURE_SPEECH_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
AZURE_SPEECH_TRANSCRIBE_PATH = "/speechtotext/transcriptions:transcribe"
AZURE_AUTHENTICATION_METHOD = "InteractiveBrowserCredential"
BROWSER_AUTH_TIMEOUT_SECONDS = 300
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 413}


class AccessTokenLike(Protocol):
    token: str


class BrowserCredentialLike(Protocol):
    def authenticate(self, *, scopes: Iterable[str]) -> object: ...

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessTokenLike: ...


class HttpClientLike(Protocol):
    def post(self, url: str, **kwargs: Any) -> httpx.Response: ...

    def close(self) -> None: ...


CredentialFactory = Callable[[], BrowserCredentialLike]
ClientFactory = Callable[[float], HttpClientLike]
Sleep = Callable[[float], None]
Jitter = Callable[[float, float], float]


@dataclass(frozen=True)
class AzureTranscription:
    segments: list[Segment]
    attempts: int
    retries: int
    request_id: str | None


def _default_credential_factory() -> BrowserCredentialLike:
    from azure.identity import InteractiveBrowserCredential

    return cast(
        BrowserCredentialLike,
        InteractiveBrowserCredential(
            disable_automatic_authentication=True,
            cache_persistence_options=None,
            timeout=BROWSER_AUTH_TIMEOUT_SECONDS,
        ),
    )


def _default_client_factory(timeout_seconds: float) -> HttpClientLike:
    return cast(HttpClientLike, httpx.Client(timeout=httpx.Timeout(timeout_seconds)))


def endpoint_type(endpoint: str | None) -> str:
    if not endpoint:
        return "not-configured"
    hostname = httpx.URL(endpoint).host.lower()
    if hostname.endswith(".cognitiveservices.azure.com"):
        return "azure-custom-subdomain"
    if hostname.endswith(".api.cognitive.microsoft.com"):
        return "azure-regional"
    return "custom-https"


def _safe_header_value(response: httpx.Response, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = response.headers.get(name)
        if value:
            return "".join(character for character in value if character.isprintable())[:200]
    return None


def _request_id(response: httpx.Response) -> str | None:
    return _safe_header_value(
        response,
        ("apim-request-id", "x-ms-request-id", "request-id", "x-request-id"),
    )


def _azure_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else payload.get("code")
    if not isinstance(code, str):
        return None
    return "".join(character for character in code if character.isalnum() or character in "._-")[
        :100
    ]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _safe_error_message(
    *,
    status_code: int,
    error_code: str | None,
    request_id: str | None,
    attempts: int,
) -> str:
    fields = [f"HTTP status={status_code}", f"attempts={attempts}"]
    if error_code:
        fields.append(f"azure_error_code={error_code}")
    if request_id:
        fields.append(f"request_id={request_id}")
    return "Azure Speech transcription failed (" + ", ".join(fields) + ")"


def _segments_from_response(response: httpx.Response) -> list[Segment]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise AzureSpeechError(
            "Azure Speech returned a successful response with invalid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("phrases"), list):
        raise AzureSpeechError(
            "Azure Speech returned a successful response without a phrases array"
        )

    segments: list[Segment] = []
    for index, phrase in enumerate(payload["phrases"], start=1):
        if not isinstance(phrase, dict):
            raise AzureSpeechError("Azure Speech returned an invalid phrase record")
        offset = phrase.get("offsetMilliseconds")
        duration = phrase.get("durationMilliseconds")
        text = phrase.get("text")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, (int, float))
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not isinstance(text, str)
            or offset < 0
            or duration < 0
        ):
            raise AzureSpeechError("Azure Speech returned an invalid phrase record")
        speaker_value = phrase.get("speaker")
        speaker = None if speaker_value is None else str(speaker_value)
        start = float(offset) / 1000.0
        segments.append(
            Segment(
                index=index,
                start=start,
                end=start + (float(duration) / 1000.0),
                text=text,
                chunk="azure-whole-file",
                speaker=speaker,
            )
        )
    return segments


class AzureSpeechFastAdapter:
    def __init__(
        self,
        *,
        config: AzureSpeechTranscriptionConfig,
        logger: logging.Logger,
        credential_factory: CredentialFactory = _default_credential_factory,
        client_factory: ClientFactory = _default_client_factory,
        sleep: Sleep = time.sleep,
        jitter: Jitter = random.uniform,
    ) -> None:
        self.config = config
        self.logger = logger
        self.credential_factory = credential_factory
        self.client_factory = client_factory
        self.sleep = sleep
        self.jitter = jitter

    def transcribe(self, upload_audio: Path) -> AzureTranscription:
        endpoint = self.config.endpoint.rstrip("/")
        api_version = self.config.api_version
        locale = self.config.locale
        diarization = self.config.diarization_enabled
        max_speakers = self.config.max_speakers
        timeout = float(self.config.timeout_seconds)
        max_retries = self.config.max_retries

        definition: dict[str, Any] = {"locales": [locale]}
        if diarization:
            definition["diarization"] = {
                "enabled": True,
                "maxSpeakers": max_speakers,
            }

        try:
            credential = self.credential_factory()
        except Exception as exc:
            raise AzureSpeechError(
                "Azure Speech Entra credential initialization failed before submission"
            ) from exc
        client: HttpClientLike | None = None
        try:
            try:
                self.logger.info(
                    "Opening your browser: select the Microsoft account with access to "
                    "the configured Speech resource (authentication timeout: %d seconds)",
                    BROWSER_AUTH_TIMEOUT_SECONDS,
                )
                # A fresh browser credential is created for each submission, without an
                # authentication record or persistent cache. Its interactive flow uses
                # prompt=select_account; HTTP retries below reuse this selected identity.
                credential.authenticate(scopes=[AZURE_SPEECH_TOKEN_SCOPE])
                token = credential.get_token(AZURE_SPEECH_TOKEN_SCOPE).token
            except Exception as exc:
                raise AzureSpeechError(
                    "Azure Speech browser authentication failed or timed out before submission. "
                    "Complete account selection in the browser and retry; "
                    "a browser and a local callback connection must be available."
                ) from exc
            self.logger.info("Browser account selection completed; preparing the Speech upload")
            try:
                client = self.client_factory(timeout)
            except Exception as exc:
                raise AzureSpeechError(
                    "Azure Speech HTTP client initialization failed before submission"
                ) from exc
            url = endpoint + AZURE_SPEECH_TRANSCRIBE_PATH
            total_attempts = max_retries + 1
            for attempt in range(1, total_attempts + 1):
                self.logger.info(
                    "Submitting FLAC audio to Azure Speech: attempt %d/%d",
                    attempt,
                    total_attempts,
                )
                with upload_audio.open("rb") as audio_stream:
                    try:
                        response = client.post(
                            url,
                            params={"api-version": api_version},
                            headers={"Authorization": f"Bearer {token}"},
                            data={"definition": json.dumps(definition, separators=(",", ":"))},
                            files={
                                "audio": (
                                    "normalized_16k_mono.flac",
                                    audio_stream,
                                    "audio/flac",
                                )
                            },
                        )
                    except httpx.RequestError as exc:
                        audio_position = audio_stream.tell()
                        definitely_pre_upload = isinstance(
                            exc,
                            (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
                        )
                        definitely_incomplete = (
                            isinstance(exc, (httpx.WriteError, httpx.WriteTimeout))
                            and audio_position < upload_audio.stat().st_size
                        )
                        if definitely_pre_upload or definitely_incomplete:
                            if attempt >= total_attempts:
                                phase = (
                                    "before upload"
                                    if definitely_pre_upload
                                    else "during a confirmed incomplete upload"
                                )
                                raise AzureSpeechError(
                                    f"Azure Speech connection failed {phase} (attempts={attempt})"
                                ) from exc
                            delay = min(30.0, 2.0 ** (attempt - 1))
                            delay += self.jitter(0.0, delay * 0.25)
                            self.logger.warning(
                                "Azure Speech safe transport retry "
                                "(attempt=%d, delay_seconds=%.1f)",
                                attempt,
                                delay,
                            )
                            self.sleep(delay)
                            continue
                        raise AzureSubmissionOutcomeUnknownError(
                            "Azure Speech submission_outcome_unknown: the upload may have "
                            "completed, so it was not retried automatically "
                            f"(attempts={attempt})"
                        ) from exc

                request_id = _request_id(response)
                error_code = _azure_error_code(response) if response.status_code >= 400 else None
                if response.status_code == 200:
                    segments = _segments_from_response(response)
                    return AzureTranscription(
                        segments=segments,
                        attempts=attempt,
                        retries=attempt - 1,
                        request_id=request_id,
                    )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < total_attempts:
                    retry_after = _retry_after_seconds(response)
                    base_delay = min(30.0, 2.0 ** (attempt - 1))
                    delay = (
                        retry_after
                        if retry_after is not None
                        else base_delay + self.jitter(0.0, base_delay * 0.25)
                    )
                    self.logger.warning(
                        "Azure Speech retryable response: status=%d, azure_error_code=%s, "
                        "request_id=%s, attempt=%d, delay_seconds=%.1f",
                        response.status_code,
                        error_code or "none",
                        request_id or "none",
                        attempt,
                        delay,
                    )
                    self.sleep(delay)
                    continue
                raise AzureSpeechError(
                    _safe_error_message(
                        status_code=response.status_code,
                        error_code=error_code,
                        request_id=request_id,
                        attempts=attempt,
                    )
                )
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()
            close = getattr(credential, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

        raise AzureSpeechError("Azure Speech transcription ended without a result")
