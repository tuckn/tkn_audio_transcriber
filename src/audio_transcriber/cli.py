from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    ResolvedConfig,
    default_user_config_path,
    initialize_user_config,
    migrate_config_file,
    resolve_config,
)
from .errors import AudioTranscriberError
from .job_state import list_jobs
from .logging_config import configure_logging
from .maintenance import cleanup_jobs
from .model_store import download_model
from .pipeline import TranscriptionPipeline
from .validation import validate_artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tkn-audio-transcriber",
        description=(
            "Create Markdown, SRT, and JSONL transcripts with local or Azure Speech ASR."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        help="Explicit YAML config (after user and working-directory config).",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Show errors only.")
    verbosity.add_argument("-v", "--verbose", action="store_true", help="Show debug details.")
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser(
        "config", help="Initialize, inspect, or migrate configuration."
    )
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    config_init = config_commands.add_parser(
        "init", help="Create the user configuration from the packaged example."
    )
    config_init.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="Config path (default: ~/.tkn/audio_transcriber/config.yaml).",
    )
    config_init.add_argument(
        "--force",
        action="store_true",
        help="Back up and replace an existing config with different content.",
    )
    config_init.add_argument(
        "--dry-run", action="store_true", help="Print the planned action without writing."
    )
    config_commands.add_parser("show", help="Print values and their source as JSON.")
    config_migrate = config_commands.add_parser(
        "migrate", help="Migrate one config to the current schema with a backup."
    )
    config_migrate.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="Config path (default: ~/.tkn/audio_transcriber/config.yaml).",
    )
    config_migrate.add_argument(
        "--dry-run", action="store_true", help="Validate and print the plan without writing."
    )

    model_parser = commands.add_parser("model", help="Manage local faster-whisper models.")
    model_commands = model_parser.add_subparsers(dest="model_command", required=True)
    model_download = model_commands.add_parser("download", help="Download a model explicitly.")
    model_download.add_argument("model", help="Known model name or Hugging Face repository ID.")
    model_download.add_argument(
        "--model-dir", type=Path, help="Override the configured model directory."
    )
    model_download.add_argument(
        "--cache-dir", type=Path, help="Override the configured Hugging Face cache."
    )
    model_download.add_argument(
        "--dry-run", action="store_true", help="Print the download plan without writing."
    )

    transcribe = commands.add_parser(
        "transcribe", help="Normalize, split, transcribe, verify, and commit outputs."
    )
    transcribe.add_argument(
        "audio",
        type=Path,
        metavar="SOURCE",
        help="Source audio or video file with an audio stream (never modified).",
    )
    transcribe.add_argument("--output-dir", type=Path, help="Transcript output directory.")
    transcribe.add_argument(
        "--provider",
        choices=("faster-whisper", "azure-speech-fast"),
        help="ASR provider; Azure uploads normalized audio only with explicit approval.",
    )
    transcribe.add_argument("--model", help="Model name or local model directory.")
    transcribe.add_argument("--language", help="Language code, for example ja or en.")
    transcribe.add_argument("--chunk-seconds", type=int, help="Chunk length in seconds.")
    transcribe.add_argument("--beam-size", type=int, help="Whisper beam size.")
    transcribe.add_argument("--compute-type", help="CTranslate2 compute type.")
    transcribe.add_argument("--device", help="faster-whisper device, usually cpu or cuda.")
    transcribe.add_argument("--model-dir", type=Path, help="Local model root.")
    transcribe.add_argument("--cache-dir", type=Path, help="Hugging Face cache root.")
    transcribe.add_argument("--state-dir", type=Path, help="Durable checkpoint root.")
    transcribe.add_argument("--ffmpeg-executable", help="ffmpeg executable or absolute path.")
    transcribe.add_argument(
        "--subprocess-timeout-seconds", type=int, help="ffmpeg timeout in seconds."
    )
    transcribe.add_argument(
        "--heartbeat-seconds",
        type=int,
        help="Progress heartbeat interval while a chunk is being recognized.",
    )
    transcribe.add_argument(
        "--keep-working-files",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Keep normalized audio and chunks after successful verification.",
    )
    transcribe.add_argument(
        "--azure-speech-endpoint",
        help="Azure Speech HTTPS resource endpoint (never a subscription key).",
    )
    transcribe.add_argument("--azure-speech-region", help="Azure Speech resource region.")
    transcribe.add_argument("--azure-speech-api-version", help="Speech REST API version.")
    transcribe.add_argument("--azure-speech-locale", help="Speech locale, for example ja-JP.")
    transcribe.add_argument(
        "--azure-speech-diarization-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable Azure speaker diarization.",
    )
    transcribe.add_argument(
        "--azure-speech-max-speakers",
        type=int,
        help="Expected maximum speakers for diarization (2-35).",
    )
    transcribe.add_argument(
        "--azure-speech-timeout-seconds", type=int, help="Azure request timeout in seconds."
    )
    transcribe.add_argument(
        "--azure-speech-max-retries",
        type=int,
        help="Maximum safe automatic Azure retries.",
    )
    transcribe.add_argument(
        "--allow-cloud-upload",
        action="store_true",
        help=(
            "Approve uploading the normalized audio for this run only; cannot be saved in config."
        ),
    )
    transcribe.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and print planned outputs without writing.",
    )
    transcribe.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing differing or incomplete outputs after successful processing.",
    )

    validate = commands.add_parser("validate", help="Validate output hashes and manifest.")
    validate.add_argument("manifest", type=Path, help="Transcript manifest JSON.")
    validate.add_argument(
        "--verify-source",
        action="store_true",
        help="Also re-hash the original source media file.",
    )
    cleanup = commands.add_parser("cleanup", help="Plan or remove validated completed job state.")
    cleanup.add_argument("--state-dir", type=Path, help="Durable checkpoint root.")
    cleanup.add_argument(
        "--older-than-days",
        type=int,
        default=30,
        help="Only include completed jobs at least this many days old (default: 30).",
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Delete listed job directories; without this option cleanup is read-only.",
    )

    status = commands.add_parser(
        "status", help="Show durable transcription job state and heartbeat details."
    )
    status.add_argument("--state-dir", type=Path, help="Durable checkpoint root.")
    return parser


def _json_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _path_value(value: Path | None) -> str | None:
    return None if value is None else str(value)


def _resolve(args: argparse.Namespace, overrides: dict[str, Any] | None = None) -> ResolvedConfig:
    return resolve_config(
        cwd=Path.cwd(),
        explicit_config=args.config,
        cli_overrides=overrides,
    )


def _config_command_path(args: argparse.Namespace) -> Path:
    path = args.path or args.config or default_user_config_path()
    if args.path is not None and args.config is not None:
        raise AudioTranscriberError(
            "Specify a config path either as --config or as the command path, not both."
        )
    return path


def _warn_in_memory_migrations(config: ResolvedConfig, logger: logging.Logger) -> None:
    for source in config.config_sources:
        migration = source["migration"]
        if migration is not None:
            command_path = json.dumps(str(source["path"]), ensure_ascii=False)
            logger.warning(
                "Config %s uses legacy schema_version %r; interpreted as %s in memory. "
                "Run `tkn-audio-transcriber config migrate %s` to update it.",
                source["path"],
                migration["from_version"],
                migration["to_version"],
                command_path,
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logger = configure_logging(quiet=args.quiet, verbose=args.verbose)
    try:
        if args.command == "config":
            if args.config_command == "init":
                _json_result(
                    initialize_user_config(
                        _config_command_path(args),
                        force=args.force,
                        dry_run=args.dry_run,
                    )
                )
                return 0
            if args.config_command == "show":
                config = _resolve(args)
                _warn_in_memory_migrations(config, logger)
                _json_result(config.display())
                return 0
            if args.config_command == "migrate":
                _json_result(migrate_config_file(_config_command_path(args), dry_run=args.dry_run))
                return 0
        if args.command == "model" and args.model_command == "download":
            overrides = {
                "model_dir": _path_value(args.model_dir),
                "cache_dir": _path_value(args.cache_dir),
            }
            config = _resolve(args, overrides)
            _warn_in_memory_migrations(config, logger)
            model_dir = config.path("model_dir")
            cache_dir = config.path("cache_dir")
            assert model_dir is not None and cache_dir is not None
            _json_result(
                download_model(
                    model=args.model,
                    model_dir=model_dir,
                    cache_dir=cache_dir,
                    dry_run=args.dry_run,
                    logger=logger,
                )
            )
            return 0
        if args.command == "transcribe":
            overrides = {
                "output_dir": _path_value(args.output_dir),
                "provider": args.provider,
                "model": args.model,
                "language": args.language,
                "chunk_seconds": args.chunk_seconds,
                "beam_size": args.beam_size,
                "compute_type": args.compute_type,
                "device": args.device,
                "model_dir": _path_value(args.model_dir),
                "cache_dir": _path_value(args.cache_dir),
                "state_dir": _path_value(args.state_dir),
                "ffmpeg_executable": args.ffmpeg_executable,
                "subprocess_timeout_seconds": args.subprocess_timeout_seconds,
                "heartbeat_seconds": args.heartbeat_seconds,
                "keep_working_files": args.keep_working_files,
                "azure_speech_endpoint": args.azure_speech_endpoint,
                "azure_speech_region": args.azure_speech_region,
                "azure_speech_api_version": args.azure_speech_api_version,
                "azure_speech_locale": args.azure_speech_locale,
                "azure_speech_diarization_enabled": (
                    args.azure_speech_diarization_enabled
                ),
                "azure_speech_max_speakers": args.azure_speech_max_speakers,
                "azure_speech_timeout_seconds": args.azure_speech_timeout_seconds,
                "azure_speech_max_retries": args.azure_speech_max_retries,
            }
            config = _resolve(args, overrides)
            _warn_in_memory_migrations(config, logger)
            result = TranscriptionPipeline(config=config, logger=logger).transcribe(
                args.audio,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
                allow_cloud_upload=args.allow_cloud_upload,
            )
            _json_result(result.to_dict())
            return 0
        if args.command == "validate":
            _json_result(validate_artifact(args.manifest, verify_source=args.verify_source))
            return 0
        if args.command == "cleanup":
            config = _resolve(args, {"state_dir": _path_value(args.state_dir)})
            _warn_in_memory_migrations(config, logger)
            state_dir = config.path("state_dir")
            assert state_dir is not None
            _json_result(
                cleanup_jobs(
                    state_dir,
                    older_than_days=args.older_than_days,
                    apply=args.apply,
                )
            )
            return 0
        if args.command == "status":
            config = _resolve(args, {"state_dir": _path_value(args.state_dir)})
            _warn_in_memory_migrations(config, logger)
            state_dir = config.path("state_dir")
            assert state_dir is not None
            _json_result(list_jobs(state_dir))
            return 0
        parser.error("Unsupported command")
        return 2
    except AudioTranscriberError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.error("Interrupted. Durable checkpoint files were kept for resume.")
        return 130
    except Exception as exc:
        if args.verbose:
            logger.exception("Unexpected failure")
        else:
            logger.error("Unexpected failure: %s. Re-run with --verbose for details.", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
