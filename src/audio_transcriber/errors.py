class AudioTranscriberError(Exception):
    """Expected, actionable CLI error."""


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
