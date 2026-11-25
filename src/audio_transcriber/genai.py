"""Generative AI connections from this CLI's config; Bridge's own config is never read."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import ResolvedConfig
from .errors import ConfigError

if TYPE_CHECKING:
    from tkn_genai_bridge import Profile


def selected_connection_name(config: ResolvedConfig, override: str | None) -> str:
    """The `genai_profiles` name given on the command line, else `generation.genai_profile`."""
    name = override if override is not None else config.generation.get("genai_profile")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("Set generation.genai_profile or pass --genai-profile")
    return name


def connection(config: ResolvedConfig, name: str) -> Profile:
    """Validate one connection with tkn-genai-bridge, without reading Bridge's config files."""
    # Import lazily so ordinary transcription does not initialize the generation stack.
    from tkn_genai_bridge import Profile

    settings = config.genai_profiles.get(name)
    if not isinstance(settings, dict):
        available = ", ".join(sorted(config.genai_profiles)) or "(none)"
        raise ConfigError(
            f"genai_profile {name!r} is not defined in genai_profiles; defined: {available}"
        )
    try:
        return Profile.model_validate(settings)
    except ValueError as exc:
        # Report field locations and error types only. Messages from custom validators
        # can quote the values, which may hold private settings.
        errors = getattr(exc, "errors", None)
        details = (
            "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or name} ({error['type']})"
                for error in errors(include_input=False, include_url=False)
            )
            if callable(errors)
            else "invalid settings"
        )
        raise ConfigError(
            f"Invalid genai_profiles.{name}: {details}; see the tkn-genai-bridge Profile settings"
        ) from None


def require_provider_executable(profile: Profile, name: str) -> None:
    """Find a CLI provider's executable now, the way tkn-genai-bridge will when it runs."""
    from tkn_genai_bridge import GenAIError
    from tkn_genai_bridge.models import CLI_EXECUTABLES
    from tkn_genai_bridge.providers.cli import resolve_executable

    if profile.provider not in CLI_EXECUTABLES:
        return
    try:
        resolve_executable(profile)
    except GenAIError:
        raise ConfigError(
            f"The {profile.provider} CLI for genai_profiles.{name} was not found; install it, "
            "add it to PATH, or set cli.executable"
        ) from None
