from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

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
from .output import emit
from .pipeline import TranscriptionPipeline
from .validation import validate_artifact


class UsageError(AudioTranscriberError):
    """An argument error rendered through the selected result format."""


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="Print one JSON result instead of the default key=value list; no effect on files.",
        )
        logs = self.add_mutually_exclusive_group()
        logs.add_argument(
            "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
            help="Show only errors on stderr; always print the final result.",
        )
        logs.add_argument(
            "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
            help="Show debug diagnostics on stderr; keep the same final result.",
        )

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _target_paths(args: argparse.Namespace) -> list[str]:
    paths: list[str] = []
    for key in ("audio", "transcript", "manifest", "path", "config", "output_dir",
                "state_dir", "context_file", "model_dir", "cache_dir", "phrase_file"):
        value = getattr(args, key, None)
        for path in value if isinstance(value, list) else [value]:
            if path is not None:
                paths.append(str(Path(path).expanduser().absolute()))
    return list(dict.fromkeys(paths))


def _parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = Parser(
        prog="tkn-audio-transcriber",
        description=("Create Markdown, SRT, and JSONL transcripts with local or Azure Speech ASR."),
    )
    parser.set_defaults(json=False, quiet=False, verbose=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    config_help = "Explicit YAML config (after user and working-directory config)."
    parser.add_argument("--config", type=Path, help=config_help)
    profile_help = (
        "Transcription profile such as local/gpu-quality or cloud/azure-ja; "
        "an unqualified unique name is also accepted."
    )
    parser.add_argument("--profile", metavar="MODE/NAME", help=profile_help)
    command_options = argparse.ArgumentParser(add_help=False)
    command_options.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help=config_help
    )
    command_options.add_argument(
        "--profile", metavar="MODE/NAME", default=argparse.SUPPRESS, help=profile_help
    )
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
    config_commands.add_parser(
        "list", parents=[command_options],
        help="List resolved settings and their sources as key=value lines."
    )
    config_commands.add_parser(
        "profiles", parents=[command_options],
        help="List available transcription profiles and the active selection."
    )
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
    model_download = model_commands.add_parser(
        "download", parents=[command_options], help="Download a model explicitly."
    )
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

    transcribe = argparse.ArgumentParser(add_help=False, parents=[command_options])
    transcribe.add_argument(
        "audio",
        type=Path,
        metavar="SOURCE",
        help="Source audio or video file with an audio stream (never modified).",
    )
    transcribe.add_argument(
        "--phrase-file", type=Path, action="append",
        help="Reviewed UTF-8 vocabulary (one phrase per line); repeat to add files to config.",
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
        help="Keep working WAV/FLAC and chunks; final audio.flac is always retained.",
    )
    transcribe.add_argument(
        "--azure-speech-endpoint",
        help="Azure Speech HTTPS resource endpoint (never a subscription key).",
    )
    transcribe.add_argument("--azure-speech-region", help="Azure Speech resource region.")
    transcribe.add_argument("--azure-speech-api-version", help="Speech REST API version.")
    transcribe.add_argument("--azure-speech-locale", help="Speech locale, for example ja-JP.")
    transcribe.add_argument(
        "--azure-speech-profanity-filter-mode",
        choices=["None", "Masked", "Removed", "Tags"],
        help="Profanity handling (default: None, preserve recognized text).",
    )
    transcribe.add_argument(
        "--azure-speech-reuse-cached-credentials",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Reuse encrypted credentials; disable to select an account again.",
    )
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
            "Approve uploading FLAC audio for this run only; cannot be saved in config. "
            "Browser sign-in is needed only when cached credentials cannot be reused."
        ),
    )
    transcribe.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and print planned outputs without writing.",
    )

    transcribe_command = commands.add_parser(
        "transcribe", parents=[transcribe],
        help="Normalize, split, transcribe, verify, and commit outputs.",
    )
    transcribe_command.add_argument(
        "--output-dir", type=Path,
        help=("Output root; creates a source-name/hash folder. "
              "--suggest-phrases adds vocabulary/ inside that folder."),
    )
    transcribe_command.add_argument(
        "--overwrite", action="store_true",
        help="Repair this transcript after processing; shared audio is never overwritten.",
    )
    transcribe_command.add_argument(
        "--suggest-phrases", action="store_true",
        help=("Also generate vocabulary files and exit; "
              "uses configured output root; excludes --overwrite."),
    )
    transcribe_command.add_argument(
        "--generation-profile", help="Generation profile (requires --suggest-phrases)."
    )
    transcribe_command.add_argument(
        "--context-file", type=Path, help="UTF-8 background text (requires --suggest-phrases)."
    )
    transcribe_command.add_argument(
        "--max-candidates", type=int, help="Maximum candidates (requires --suggest-phrases)."
    )

    suggest = commands.add_parser(
        "suggest-phrases", parents=[command_options],
        help="Generate vocabulary files from saved transcript JSONL and exit."
    )
    suggest.add_argument("transcript", type=Path, help="Transcript JSONL produced by this CLI.")
    suggest.add_argument("--output-dir", type=Path, required=True, help="New review directory.")
    suggest.add_argument("--generation-profile", help="Application generation profile name.")
    suggest.add_argument(
        "--context-file", type=Path, help="Optional UTF-8 meeting background text."
    )
    suggest.add_argument("--max-candidates", type=int, help="Maximum candidates (default: config).")
    suggest.add_argument(
        "--dry-run", action="store_true", help="Plan without generation or writes."
    )

    validate = commands.add_parser("validate", help="Validate output hashes and manifest.")
    validate.add_argument("manifest", type=Path, help="Transcript manifest JSON.")
    validate.add_argument(
        "--verify-source",
        action="store_true",
        help="Also re-hash the original source media file.",
    )
    cleanup = commands.add_parser(
        "cleanup", parents=[command_options],
        help="Plan or remove validated completed job state."
    )
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
        "status", parents=[command_options],
        help="Show durable transcription job state and heartbeat details."
    )
    status.add_argument("--state-dir", type=Path, help="Durable checkpoint root.")
    return parser


def _path_value(value: Path | None) -> str | None:
    return None if value is None else str(value)


def _resolve(args: argparse.Namespace, overrides: dict[str, Any] | None = None) -> ResolvedConfig:
    return resolve_config(
        cwd=Path.cwd(),
        explicit_config=args.config,
        profile=args.profile,
        cli_overrides=overrides,
        phrase_files=getattr(args, "phrase_file", None),
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
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raw = list(sys.argv[1:] if argv is None else argv)
    # A minimal parse preserves display intent even if the real command is invalid.
    flags = raw[:raw.index("--")] if "--" in raw else raw
    options = argparse.Namespace(
        json=any(flag == "--json" or flag.startswith("--json=") for flag in flags),
        quiet=any(flag in {"-q", "--quiet"} for flag in flags),
        verbose=any(flag in {"-v", "--verbose"} for flag in flags),
    )
    logger = configure_logging(quiet=options.quiet, verbose=options.verbose)
    parser = _parser()
    args = argparse.Namespace()

    def show(payload: dict[str, Any]) -> None:
        emit(payload, json_output=options.json)

    def failure(exc: BaseException, code: int, status: str = "failed") -> int:
        paths = _target_paths(args)
        if isinstance(exc, AudioTranscriberError) and exc.target_path is not None:
            paths.append(str(exc.target_path.expanduser().absolute()))
        if isinstance(exc, OSError) and exc.filename:
            paths.append(str(Path(exc.filename).absolute()))
        if getattr(args, "command", None) == "config" or type(exc).__name__ == "ConfigError":
            for path in (default_user_config_path(), Path.cwd() / ".tkn" / "config.yaml"):
                if path.is_file():
                    paths.append(str(path.absolute()))
        show({
            "status": status,
            "exit_code": code,
            "command": getattr(args, "command", None),
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "target_paths": list(dict.fromkeys(paths)),
        })
        return code

    try:
        args = parser.parse_args(raw, namespace=args)
        if args.quiet and args.verbose:
            parser.error("--quiet and --verbose cannot be combined")
        if args.command == "transcribe":
            if args.suggest_phrases:
                if args.overwrite:
                    parser.error("--suggest-phrases cannot be combined with --overwrite")
            elif any(value is not None for value in (
                args.generation_profile, args.context_file, args.max_candidates
            )):
                parser.error("Generation options require --suggest-phrases")
        if args.command == "config":
            if args.config_command == "init":
                show(
                    initialize_user_config(
                        _config_command_path(args),
                        force=args.force,
                        dry_run=args.dry_run,
                    )
                )
                return 0
            if args.config_command == "list":
                config = _resolve(args)
                _warn_in_memory_migrations(config, logger)
                logger.info("Showing resolved configuration")
                show(config.display())
                return 0
            if args.config_command == "profiles":
                config = _resolve(args)
                _warn_in_memory_migrations(config, logger)
                show(config.profiles_display())
                return 0
            if args.config_command == "migrate":
                show(migrate_config_file(_config_command_path(args), dry_run=args.dry_run))
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
            show(
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
                "azure_speech_diarization_enabled": (args.azure_speech_diarization_enabled),
                "azure_speech_max_speakers": args.azure_speech_max_speakers,
                "azure_speech_timeout_seconds": args.azure_speech_timeout_seconds,
                "azure_speech_max_retries": args.azure_speech_max_retries,
                "azure_speech_profanity_filter_mode": args.azure_speech_profanity_filter_mode,
                "azure_speech_reuse_cached_credentials": args.azure_speech_reuse_cached_credentials,
            }
            config = _resolve(args, overrides)
            _warn_in_memory_migrations(config, logger)
            if args.suggest_phrases:
                from .prepare import prepare_transcription

                payload = prepare_transcription(
                    args.audio, config=config, logger=logger,
                    generation_profile=args.generation_profile,
                    context_file=args.context_file, max_candidates=args.max_candidates,
                    dry_run=args.dry_run, allow_cloud_upload=args.allow_cloud_upload,
                )
                show(payload)
                return 0
            result = TranscriptionPipeline(config=config, logger=logger).transcribe(
                args.audio,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
                allow_cloud_upload=args.allow_cloud_upload,
            )
            show(result.to_dict())
            return 0
        if args.command == "suggest-phrases":
            from .phrase_suggestions import suggest_phrases

            config = _resolve(args)
            _warn_in_memory_migrations(config, logger)
            logger.info(
                "Planning phrase candidates" if args.dry_run else "Generating phrase candidates"
            )
            payload = suggest_phrases(
                args.transcript, output_dir=args.output_dir, config=config,
                generation_profile=args.generation_profile, context_file=args.context_file,
                max_candidates=args.max_candidates, dry_run=args.dry_run,
            )
            show(payload)
            return 0
        if args.command == "validate":
            show(validate_artifact(args.manifest, verify_source=args.verify_source))
            return 0
        if args.command == "cleanup":
            config = _resolve(args, {"state_dir": _path_value(args.state_dir)})
            _warn_in_memory_migrations(config, logger)
            state_dir = config.path("state_dir")
            assert state_dir is not None
            show(
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
            show(list_jobs(state_dir))
            return 0
        parser.error("Unsupported command")
        return 2
    except AudioTranscriberError as exc:
        logger.error("%s", exc)
        return failure(exc, 2)
    except KeyboardInterrupt as exc:
        logger.error("Interrupted. Durable checkpoint files were kept for resume.")
        return failure(exc, 130, "interrupted")
    except Exception as exc:
        if options.verbose:
            logger.exception("Unexpected failure")
        else:
            logger.error("Unexpected failure: %s. Re-run with --verbose for details.", exc)
        return failure(exc, 1)


if __name__ == "__main__":
    raise SystemExit(main())
