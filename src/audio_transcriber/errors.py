from pathlib import Path


class AudioTranscriberError(Exception):
    """Expected, actionable CLI error with optional copyable target metadata."""

    def __init__(self, message: str, *, target_path: Path | None = None) -> None:
        super().__init__(message)
        self.target_path = target_path


class ConfigError(AudioTranscriberError):
    """Configuration is invalid."""


class ValidationError(AudioTranscriberError):
    """Input or output validation failed."""


class ExternalProcessError(AudioTranscriberError):
    """An external process failed."""


class CloudUploadApprovalError(AudioTranscriberError):
    """A required per-run cloud upload approval is missing."""


class AzureSpeechError(AudioTranscriberError):
    """Azure Speech returned a safe, actionable failure."""


class AzureSubmissionOutcomeUnknownError(AzureSpeechError):
    """An Azure upload may have completed, so automatic retry is unsafe."""
