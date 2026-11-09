"""Archive and transcribe new media files from configured source folders."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from stat import FILE_ATTRIBUTE_REPARSE_POINT
from typing import Any, Protocol

from .config import AzureSpeechTranscriptionConfig, ResolvedConfig, SourceConfig
from .errors import AudioTranscriberError, CloudUploadApprovalError, ConfigError, ValidationError
from .io_utils import atomic_copy_file, sha256_file
from .logging_config import log_success
from .models import TranscriptionResult
from .pipeline import TranscriptionPipeline, dry_run_plan, planned_outputs

ARCHIVE_NAME_FORMAT = "%Y%m%dT%H%M%S%z"
_MAX_ARCHIVE_NAME_ATTEMPTS = 1000


class Transcriber(Protocol):
    def transcribe(
        self, source: Path, *, dry_run: bool, overwrite: bool, allow_cloud_upload: bool = False
    ) -> TranscriptionResult: ...


# (profile selector or None for the active profile, output_dir or None) -> config
ConfigResolver = Callable[[str | None, str | None], ResolvedConfig]
PipelineFactory = Callable[[ResolvedConfig, logging.Logger], Transcriber]


def _default_pipeline(config: ResolvedConfig, logger: logging.Logger) -> Transcriber:
    return TranscriptionPipeline(config=config, logger=logger)


def select_sources(
    configured: Sequence[SourceConfig], names: Sequence[str]
) -> list[SourceConfig]:
    if not configured:
        raise ConfigError("No sources are configured; add a sources entry to the config file")
    if not names:
        return list(configured)
    by_name = {source.name: source for source in configured}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        available = ", ".join(by_name)
        raise ConfigError(f"Unknown source(s): {', '.join(unknown)}; available: {available}")
    return [by_name[name] for name in dict.fromkeys(names)]


def created_at(stat: os.stat_result) -> datetime:
    """Creation time when the platform records it (Windows), else modification time."""
    timestamp = getattr(stat, "st_birthtime", None)
    if not isinstance(timestamp, int | float):
        timestamp = stat.st_mtime
    return datetime.fromtimestamp(timestamp).astimezone()


def _fill(template: str, moment: datetime) -> str:
    return template.replace("{year}", f"{moment.year:04d}")


def _same_directory(first: Path, second: Path) -> bool:
    return os.path.normcase(first.resolve(strict=False)) == os.path.normcase(
        second.resolve(strict=False)
    )


def _candidates(source: SourceConfig) -> list[Path]:
    patterns = [pattern.casefold() for pattern in source.include]
    files: list[tuple[datetime, str, Path]] = []
    with os.scandir(source.input_dir) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            if not any(fnmatch.fnmatchcase(name.casefold(), pattern) for pattern in patterns):
                continue
            path = Path(entry.path)
            try:
                if not entry.is_file():
                    continue
                moment = created_at(path.stat())
            except FileNotFoundError:
                # Removed by its application while the folder was listed.
                continue
            except OSError:
                # Listed last; the per-file step reports the error and goes on.
                moment = datetime.max.replace(tzinfo=UTC)
            files.append((moment, name, path))
    # Oldest recordings first, so their outputs appear in recording order.
    return [path for _moment, _name, path in sorted(files)]


def _independent_copy(candidate: Path, source: Path, source_hash: str) -> bool:
    """True for a regular file with the same bytes that does not depend on the source."""
    # A symbolic or hard link to the input is not a copy: deleting the input would
    # leave nothing (or a dangling link) behind.
    if candidate.is_symlink() or not candidate.is_file() or os.path.samefile(candidate, source):
        return False
    return sha256_file(candidate) == source_hash


def _archive_destination(
    directory: Path, name: str, source: Path, source_hash: str
) -> tuple[Path, bool]:
    """Pick an archive path: an identical independent copy is reused, never overwritten."""
    stem, suffix = Path(name).stem, Path(name).suffix
    for attempt in range(1, _MAX_ARCHIVE_NAME_ATTEMPTS + 1):
        candidate = directory / (name if attempt == 1 else f"{stem}-{attempt}{suffix}")
        if not os.path.lexists(candidate):
            return candidate, False
        if _independent_copy(candidate, source, source_hash):
            return candidate, True
    raise ValidationError(f"Too many different files already use the archive name {name}")


def _copy_to_archive(
    path: Path, destination: Path, *, source_hash: str, stat: os.stat_result
) -> None:
    try:
        atomic_copy_file(path, destination, expected_hash=source_hash, replace=False)
    except FileExistsError:
        # Another run published the same name first; accept it only if identical.
        if not _independent_copy(destination, path, source_hash):
            raise ValidationError(
                f"Archive file appeared with different content: {destination}",
                target_path=destination,
            ) from None
        return
    # Keep the original modification time on the copy; content is already verified.
    os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _changed_error(path: Path) -> ValidationError:
    return ValidationError(
        f"Source changed after it was archived; it was kept: {path}", target_path=path
    )


def _link_error(path: Path) -> ValidationError:
    return ValidationError(
        f"Source is a link, not a recording file; it was kept: {path}", target_path=path
    )


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _NO_SHARING = 0
    _OPEN_EXISTING = 3
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    # Open a link itself instead of its target, so a link swapped in after the
    # earlier check can be detected and is never followed to another file.
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    # Reparse tags with this bit (symbolic links, junctions) stand for another
    # name. Others, such as OneDrive cloud files, are ordinary files with data.
    _REPARSE_TAG_NAME_SURROGATE = 0x20000000
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_LOCK_VIOLATION = 33
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _create_file.restype = wintypes.HANDLE
    _set_file_information = _kernel32.SetFileInformationByHandle
    _set_file_information.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    _set_file_information.restype = wintypes.BOOL
    _get_file_information = _kernel32.GetFileInformationByHandleEx
    _get_file_information.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    _get_file_information.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    def _is_link_handle(handle: int) -> bool:
        info = _FileAttributeTagInfo()
        if not _get_file_information(
            handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) and bool(
            info.ReparseTag & _REPARSE_TAG_NAME_SURROGATE
        )

    def _delete_unchanged_source(path: Path, stat: os.stat_result, source_hash: str) -> None:
        """Verify and delete under one handle that no other program can open meanwhile."""
        handle = _create_file(
            str(path), _GENERIC_READ | _DELETE, _NO_SHARING, None, _OPEN_EXISTING,
            _FILE_FLAG_SEQUENTIAL_SCAN | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
                raise ValidationError(
                    f"Source is open in another program; it was kept: {path}", target_path=path
                )
            raise ctypes.WinError(error)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _close_handle(handle)
            raise
        try:
            if _is_link_handle(handle):
                raise _link_error(path)
            current = os.fstat(descriptor)
            if current.st_size != stat.st_size or current.st_mtime_ns != stat.st_mtime_ns:
                raise _changed_error(path)
            digest = hashlib.sha256()
            while block := os.read(descriptor, 1024 * 1024):
                digest.update(block)
            if digest.hexdigest() != source_hash:
                raise _changed_error(path)
            disposition = _FileDispositionInfo(True)
            if not _set_file_information(
                handle, _FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(disposition), ctypes.sizeof(disposition),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            # The file is removed when its only handle closes, if it was marked above.
            os.close(descriptor)

else:

    def _delete_unchanged_source(path: Path, stat: os.stat_result, source_hash: str) -> None:
        # POSIX has no mandatory locks: a writer could still append between this
        # check and the unlink. min_age_seconds keeps that window unlikely.
        current = path.stat()
        if (
            current.st_size != stat.st_size
            or current.st_mtime_ns != stat.st_mtime_ns
            or sha256_file(path) != source_hash
        ):
            raise _changed_error(path)
        path.unlink()


def _process_file(
    source: SourceConfig,
    path: Path,
    stat: os.stat_result,
    *,
    resolve: ConfigResolver,
    pipeline_factory: PipelineFactory,
    logger: logging.Logger,
    allow_cloud_upload: bool,
    dry_run: bool,
    item: dict[str, Any],
) -> None:
    moment = created_at(stat)
    source_hash = sha256_file(path)
    transcribe_from = path
    archive_path: Path | None = None
    reused = False
    if source.archive is not None:
        archive_dir = Path(_fill(source.archive.directory, moment))
        if _same_directory(archive_dir, source.input_dir):
            raise ConfigError(
                f"sources.{source.name}.archive.dir must differ from input_dir: {archive_dir}"
            )
        name = (
            path.name
            if source.archive.name == "original"
            else moment.strftime(ARCHIVE_NAME_FORMAT) + path.suffix
        )
        archive_path, reused = _archive_destination(archive_dir, name, path, source_hash)
        transcribe_from = archive_path
        item["archive_path"] = str(archive_path)
        item["archive_action"] = "reuse" if reused else "copy"
    output_dir = _fill(source.output_dir, moment)
    config = resolve(source.profile, output_dir)
    item["profile"] = config.profile_selector
    item["output_dir"] = output_dir
    if dry_run:
        fingerprint, outputs = planned_outputs(
            config, transcribe_from, source_hash=source_hash, source_size=stat.st_size
        )
        item["status"] = "planned"
        item["fingerprint"] = fingerprint
        item["outputs"] = outputs.as_dict()
        item["source_deletion"] = (
            "after_success" if source.archive is not None and source.archive.delete_source
            else "never"
        )
        item["plan"] = dry_run_plan(config)
        return

    if archive_path is not None and not reused:
        logger.info("Archiving %s to %s", path, archive_path)
        _copy_to_archive(path, archive_path, source_hash=source_hash, stat=stat)
    result = pipeline_factory(config, logger).transcribe(
        transcribe_from, dry_run=False, overwrite=False, allow_cloud_upload=allow_cloud_upload
    )
    item["transcription_status"] = result.status
    item["outputs"] = result.outputs.as_dict()
    item["source_deleted"] = False
    if source.archive is not None and source.archive.delete_source:
        if path.is_symlink():
            raise _link_error(path)
        _delete_unchanged_source(path, stat, source_hash)
        item["source_deleted"] = True
        logger.info("Removed the archived source from the input folder: %s", path)
    item["status"] = "transcribed"


def run_pull(
    sources: Sequence[SourceConfig],
    *,
    resolve: ConfigResolver,
    logger: logging.Logger,
    allow_cloud_upload: bool,
    dry_run: bool,
    pipeline_factory: PipelineFactory = _default_pipeline,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    # Check every profile and upload approval before any file is copied or sent.
    for source in sources:
        config = resolve(source.profile, None)
        if (
            isinstance(config.transcription, AzureSpeechTranscriptionConfig)
            and not dry_run
            and not allow_cloud_upload
        ):
            raise CloudUploadApprovalError(
                f"Source {source.name!r} uses {config.profile_selector}, which uploads derived "
                "audio to Azure Speech. Re-run pull with --allow-cloud-upload after reviewing "
                "the source folder and provider settings."
            )

    items: list[dict[str, Any]] = []
    for source in sources:
        try:
            if not source.input_dir.is_dir():
                raise ValidationError(f"Input folder not found: {source.input_dir}")
            candidates = _candidates(source)
        except (AudioTranscriberError, OSError) as exc:
            logger.error("Could not list source %s: %s", source.name, exc)
            items.append({
                "source": source.name,
                "file": None,
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            })
            continue
        for path in candidates:
            item: dict[str, Any] = {"source": source.name, "file": str(path)}
            items.append(item)
            try:
                stat = path.stat()
                age = clock() - stat.st_mtime
                if age < source.min_age_seconds:
                    item["status"] = "skipped"
                    item["reason"] = "recently_modified"
                    item["age_seconds"] = max(0, int(age))
                    continue
                logger.info("Processing %s from source %s", path, source.name)
                _process_file(
                    source, path, stat, resolve=resolve, pipeline_factory=pipeline_factory,
                    logger=logger, allow_cloud_upload=allow_cloud_upload, dry_run=dry_run,
                    item=item,
                )
            except Exception as exc:
                # One file must not stop the others; an interrupt (BaseException) still does.
                logger.error("Could not process %s: %s: %s", path, type(exc).__name__, exc)
                item["status"] = "failed"
                item["error"] = {"type": type(exc).__name__, "message": str(exc)}

    counts = {
        status: sum(1 for item in items if item["status"] == status)
        for status in ("planned", "transcribed", "skipped", "failed")
    }
    if dry_run:
        status = "planned"
    elif not counts["failed"]:
        status = "completed"
    else:
        status = "partial" if counts["transcribed"] else "failed"
    if status == "completed":
        log_success(logger, "Pull completed: %d transcribed", counts["transcribed"])
    return {
        "status": status,
        "dry_run": dry_run,
        "sources": [source.name for source in sources],
        "items": items,
        "planned_count": counts["planned"],
        "transcribed_count": counts["transcribed"],
        "skipped_count": counts["skipped"],
        "failures_count": counts["failed"],
    }
