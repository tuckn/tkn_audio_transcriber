class AudioTranscriberError(Exception):
    """Expected, actionable CLI error."""


class ConfigError(AudioTranscriberError):
    """Configuration is invalid."""


class ValidationError(AudioTranscriberError):
    """Input or output validation failed."""


class ExternalProcessError(AudioTranscriberError):
    """An external process failed."""

