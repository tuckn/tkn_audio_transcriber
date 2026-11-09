from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import FakeAzureRecognizer, FakeFfmpeg, FakeRecognizer, make_logger

from audio_transcriber.cli import main
from audio_transcriber.config import ResolvedConfig, SourceConfig, resolve_config
from audio_transcriber.errors import CloudUploadApprovalError, ValidationError
from audio_transcriber.io_utils import sha256_file
from audio_transcriber.models import TranscriptionResult
from audio_transcriber.pipeline import TranscriptionPipeline
from audio_transcriber.pull import ARCHIVE_NAME_FORMAT, created_at, run_pull
from audio_transcriber.validation import validate_artifact

ENDPOINT = "https://example.cognitiveservices.azure.com/"


def _config_file(tmp_path: Path, source_body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "3.2.0"\n'
        "sources:\n"
        "  recordings:\n"
        "    input_dir: inbox\n"
        "    output_dir: 'out/{year}'\n" + source_body,
        encoding="utf-8",
    )
    return path


def _resolver(tmp_path: Path, config_file: Path) -> Any:
    model = tmp_path / "models" / "faster-whisper-small"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")

    def resolve(profile: str | None, output_dir: str | None) -> ResolvedConfig:
        cloud = profile is not None and profile.startswith("cloud/")
        overrides: dict[str, Any] = {
            "output_dir": output_dir,
            "state_dir": str(tmp_path / "state"),
        }
        if cloud:
            overrides["azure_speech_endpoint"] = ENDPOINT
        else:
            overrides.update({
                "model_dir": str(tmp_path / "models"),
                "cache_dir": str(tmp_path / "cache"),
                "model": "small",
                "chunk_seconds": 10,
            })
        return resolve_config(
            cwd=tmp_path, home=tmp_path / "home", explicit_config=config_file,
            profile=profile, cli_overrides=overrides,
        )

    return resolve


def _sources(resolve: Any) -> tuple[SourceConfig, ...]:
    return resolve(None, None).input_sources  # type: ignore[no-any-return]


def _local_pipeline(config: ResolvedConfig, logger: logging.Logger) -> TranscriptionPipeline:
    return TranscriptionPipeline(
        config=config, logger=logger, ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda _model, _cfg: FakeRecognizer([]),
    )


def _recording(tmp_path: Path, name: str = "Recording.flac", *, age: float = 120) -> Path:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    path = inbox / name
    path.write_bytes(b"recorded audio " + name.encode())
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _pull(tmp_path: Path, config_file: Path, **kwargs: Any) -> dict[str, Any]:
    resolve = _resolver(tmp_path, config_file)
    options: dict[str, Any] = {
        "logger": make_logger(), "allow_cloud_upload": False, "dry_run": False,
        "pipeline_factory": _local_pipeline,
    }
    options.update(kwargs)
    return run_pull(_sources(resolve), resolve=resolve, **options)


ARCHIVE = (
    "    profile: local/local-small\n"
    "    archive:\n"
    "      dir: 'archive/{year}'\n"
    "      delete_source: true\n"
)


def test_pull_archives_transcribes_and_removes_source(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    original = source.read_bytes()
    moment = created_at(source.stat())
    config_file = _config_file(tmp_path, ARCHIVE)

    payload = _pull(tmp_path, config_file)

    assert payload["status"] == "completed"
    assert payload["transcribed_count"] == 1
    item = payload["items"][0]
    archived = tmp_path / "archive" / str(moment.year) / (
        moment.strftime(ARCHIVE_NAME_FORMAT) + ".flac"
    )
    assert item["archive_path"] == str(archived)
    assert item["archive_action"] == "copy"
    assert item["source_deleted"] is True
    assert not source.exists()
    assert archived.read_bytes() == original
    text = Path(item["outputs"]["text"])
    assert text.read_text(encoding="utf-8") == (
        "text from chunk_000000.wav\ntext from chunk_000001.wav\n"
    )
    assert text.parent.parent == tmp_path / "out" / str(moment.year)
    assert text.parent.name.startswith(archived.stem + "_")
    manifest_path = Path(item["outputs"]["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source"]["path"] == str(archived)
    assert "text" in manifest["outputs"]
    assert validate_artifact(manifest_path, verify_source=True)["source_verified"] is True


def test_pull_keeps_original_modification_time_on_the_archive(tmp_path: Path) -> None:
    source = _recording(tmp_path, age=3600)
    mtime = source.stat().st_mtime_ns
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))
    assert Path(payload["items"][0]["archive_path"]).stat().st_mtime_ns == mtime


def test_pull_skips_files_that_are_still_being_written(tmp_path: Path) -> None:
    source = _recording(tmp_path, age=0)
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))

    assert payload["status"] == "completed"
    assert payload["skipped_count"] == 1
    assert payload["items"][0]["reason"] == "recently_modified"
    assert source.exists()
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "out").exists()


def test_pull_ignores_files_that_do_not_match_include(tmp_path: Path) -> None:
    _recording(tmp_path, "notes.txt")
    _recording(tmp_path, ".Recording.flac")
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))
    assert payload["items"] == []


class FailingPipeline:
    def transcribe(self, source: Path, **_kwargs: Any) -> TranscriptionResult:
        raise ValidationError("simulated recognition failure")


def test_failed_transcription_keeps_source_and_reuses_archive_on_retry(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    config_file = _config_file(tmp_path, ARCHIVE)

    failed = _pull(tmp_path, config_file, pipeline_factory=lambda _c, _l: FailingPipeline())
    assert failed["status"] == "failed"
    assert failed["failures_count"] == 1
    assert failed["items"][0]["error"]["message"] == "simulated recognition failure"
    assert source.exists()
    archived = Path(failed["items"][0]["archive_path"])
    assert archived.is_file()

    retried = _pull(tmp_path, config_file)
    assert retried["status"] == "completed"
    assert retried["items"][0]["archive_action"] == "reuse"
    assert retried["items"][0]["archive_path"] == str(archived)
    assert not source.exists()


def test_archive_name_collision_with_different_content_gets_a_suffix(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    moment = created_at(source.stat())
    taken = tmp_path / "archive" / str(moment.year) / (
        moment.strftime(ARCHIVE_NAME_FORMAT) + ".flac"
    )
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"a different recording")

    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))

    archived = Path(payload["items"][0]["archive_path"])
    assert archived.name == f"{taken.stem}-2.flac"
    assert taken.read_bytes() == b"a different recording"


def test_original_name_is_kept_when_configured(tmp_path: Path) -> None:
    _recording(tmp_path)
    payload = _pull(
        tmp_path, _config_file(tmp_path, ARCHIVE + "      name: original\n")
    )
    assert Path(payload["items"][0]["archive_path"]).name == "Recording.flac"


def test_archive_in_the_input_folder_is_refused(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    body = (
        "    profile: local/local-small\n"
        "    archive:\n"
        "      dir: inbox\n"
        "      name: original\n"
        "      delete_source: true\n"
    )
    payload = _pull(tmp_path, _config_file(tmp_path, body))

    assert payload["status"] == "failed"
    assert "must differ from input_dir" in payload["items"][0]["error"]["message"]
    assert source.read_bytes() == b"recorded audio Recording.flac"


class ChangingPipeline:
    def __init__(self, source: Path, inner: TranscriptionPipeline) -> None:
        self.source = source
        self.inner = inner

    def transcribe(self, source: Path, **kwargs: Any) -> TranscriptionResult:
        result = self.inner.transcribe(source, **kwargs)
        self.source.write_bytes(b"recording continued after archiving")
        return result


def test_source_changed_after_archiving_is_kept(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    payload = _pull(
        tmp_path, _config_file(tmp_path, ARCHIVE),
        pipeline_factory=lambda c, log: ChangingPipeline(source, _local_pipeline(c, log)),
    )

    assert payload["status"] == "failed"
    assert "changed after it was archived" in payload["items"][0]["error"]["message"]
    assert source.read_bytes() == b"recording continued after archiving"
    assert Path(payload["items"][0]["outputs"]["text"]).is_file()


def test_without_archive_files_are_transcribed_in_place_and_kept(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    payload = _pull(tmp_path, _config_file(tmp_path, "    profile: local/local-small\n"))

    item = payload["items"][0]
    assert item["status"] == "transcribed"
    assert item["source_deleted"] is False
    assert "archive_path" not in item
    assert source.exists()
    manifest = json.loads(Path(item["outputs"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["source"]["path"] == str(source)


def test_dry_run_plans_archive_and_outputs_without_writing(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    moment = created_at(source.stat())
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE), dry_run=True)

    assert payload["status"] == "planned"
    item = payload["items"][0]
    assert item["status"] == "planned"
    assert item["source_deletion"] == "after_success"
    assert Path(item["outputs"]["text"]).parent.name.startswith(
        moment.strftime(ARCHIVE_NAME_FORMAT) + "_"
    )
    assert source.exists()
    for name in ("archive", "out", "state"):
        assert not (tmp_path / name).exists()


CLOUD_ARCHIVE = ARCHIVE.replace("local/local-small", "cloud/azure-ja")


def test_cloud_source_requires_approval_before_any_file_is_copied(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    with pytest.raises(CloudUploadApprovalError, match="--allow-cloud-upload"):
        _pull(tmp_path, _config_file(tmp_path, CLOUD_ARCHIVE))
    assert source.exists()
    assert not (tmp_path / "archive").exists()


def test_cloud_source_text_output_keeps_speaker_labels(tmp_path: Path) -> None:
    _recording(tmp_path)

    def factory(config: ResolvedConfig, logger: logging.Logger) -> TranscriptionPipeline:
        return TranscriptionPipeline(
            config=config, logger=logger, ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
            azure_recognizer_factory=lambda _cfg, _log: FakeAzureRecognizer([]),
        )

    payload = _pull(
        tmp_path, _config_file(tmp_path, CLOUD_ARCHIVE),
        allow_cloud_upload=True, pipeline_factory=factory,
    )

    assert payload["status"] == "completed"
    text = Path(payload["items"][0]["outputs"]["text"])
    assert text.read_text(encoding="utf-8") == "Speaker 1: Azure text\n"


def test_missing_input_folder_is_reported(tmp_path: Path) -> None:
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))
    assert payload["status"] == "failed"
    assert "Input folder not found" in payload["items"][0]["error"]["message"]


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)


def test_cli_pull_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    _recording(tmp_path)
    config_file = _config_file(tmp_path, ARCHIVE)

    assert main(["pull", "recordings", "--config", str(config_file), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert payload["sources"] == ["recordings"]


def test_cli_pull_returns_2_when_a_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    config_file = _config_file(tmp_path, ARCHIVE)  # The inbox folder does not exist.

    assert main(["pull", "--config", str(config_file), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["failures_count"] == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--profile", "local/local-small", "pull"], "remove --profile"),
        (["pull", "missing"], "Unknown source"),
    ],
)
def test_cli_pull_rejects_invalid_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    arguments: list[str], message: str,
) -> None:
    _cli_env(tmp_path, monkeypatch)
    config_file = _config_file(tmp_path, ARCHIVE)
    assert main([*arguments, "--config", str(config_file), "--json"]) == 2
    assert message in json.loads(capsys.readouterr().out)["error"]["message"]


def test_cli_pull_without_sources_explains_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    assert main(["pull", "--json"]) == 2
    assert "No sources are configured" in json.loads(capsys.readouterr().out)["error"]["message"]


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")
def test_source_open_in_another_program_is_kept(tmp_path: Path) -> None:
    source = _recording(tmp_path)

    class HoldingPipeline:
        def __init__(self, inner: TranscriptionPipeline) -> None:
            self.inner = inner

        def transcribe(self, path: Path, **kwargs: Any) -> TranscriptionResult:
            result = self.inner.transcribe(path, **kwargs)
            # Like a recorder that still has the file open after the copy.
            self.handle = source.open("rb")  # noqa: SIM115
            return result

    holder: list[HoldingPipeline] = []

    def factory(config: ResolvedConfig, logger: logging.Logger) -> HoldingPipeline:
        holder.append(HoldingPipeline(_local_pipeline(config, logger)))
        return holder[-1]

    try:
        payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE), pipeline_factory=factory)
    finally:
        for pipeline in holder:
            pipeline.handle.close()

    assert payload["status"] == "failed"
    assert "open in another program" in payload["items"][0]["error"]["message"]
    assert source.exists()


def test_hard_link_to_the_input_is_not_treated_as_an_archive_copy(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    moment = created_at(source.stat())
    linked = tmp_path / "archive" / str(moment.year) / (
        moment.strftime(ARCHIVE_NAME_FORMAT) + ".flac"
    )
    linked.parent.mkdir(parents=True)
    os.link(source, linked)

    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))

    archived = Path(payload["items"][0]["archive_path"])
    assert archived.name == f"{linked.stem}-2.flac"
    assert not os.path.samefile(archived, linked)
    assert archived.read_bytes() == linked.read_bytes()


def test_symbolic_link_to_the_input_is_not_treated_as_an_archive_copy(tmp_path: Path) -> None:
    source = _recording(tmp_path)
    moment = created_at(source.stat())
    linked = tmp_path / "archive" / str(moment.year) / (
        moment.strftime(ARCHIVE_NAME_FORMAT) + ".flac"
    )
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("creating symbolic links needs extra privileges here")

    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))

    archived = Path(payload["items"][0]["archive_path"])
    assert archived.name == f"{linked.stem}-2.flac"
    assert not archived.is_symlink()
    assert not source.exists()
    assert archived.read_bytes() == b"recorded audio Recording.flac"


class CrashingPipeline:
    """Crashes on one recording's bytes, whatever order the files are processed in."""

    def __init__(self, crash_on: bytes, inner: TranscriptionPipeline) -> None:
        self.crash_on = crash_on
        self.inner = inner

    def transcribe(self, source: Path, **kwargs: Any) -> TranscriptionResult:
        if source.read_bytes() == self.crash_on:
            raise RuntimeError("simulated engine crash")
        return self.inner.transcribe(source, **kwargs)


def test_unexpected_engine_error_fails_one_file_and_continues(
    tmp_path: Path,
) -> None:
    first = _recording(tmp_path, "Recording.flac", age=600)
    second = _recording(tmp_path, "Recording (2).flac", age=300)
    crash_on = first.read_bytes()

    def factory(config: ResolvedConfig, logger: logging.Logger) -> Any:
        return CrashingPipeline(crash_on, _local_pipeline(config, logger))

    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE), pipeline_factory=factory)

    assert payload["status"] == "partial"
    statuses = {Path(item["file"]).name: item["status"] for item in payload["items"]}
    assert statuses == {"Recording.flac": "failed", "Recording (2).flac": "transcribed"}
    failed = next(item for item in payload["items"] if item["status"] == "failed")
    assert failed["error"] == {"type": "RuntimeError", "message": "simulated engine crash"}
    assert first.exists()
    assert not second.exists()


def test_unreadable_file_metadata_is_reported_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audio_transcriber.pull as pull_module

    _recording(tmp_path, "Recording.flac")
    _recording(tmp_path, "Recording (2).flac")
    real = pull_module.created_at

    def flaky(stat: os.stat_result) -> Any:
        if stat.st_size == len(b"recorded audio Recording.flac"):
            raise PermissionError("simulated sharing violation")
        return real(stat)

    monkeypatch.setattr(pull_module, "created_at", flaky)
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))

    statuses = {Path(item["file"]).name: item["status"] for item in payload["items"]}
    assert statuses == {"Recording.flac": "failed", "Recording (2).flac": "transcribed"}
    assert [Path(item["file"]).name for item in payload["items"]][-1] == "Recording.flac"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse point handling")
def test_link_swapped_in_before_deletion_is_not_followed(tmp_path: Path) -> None:
    import audio_transcriber.pull as pull_module

    archived = tmp_path / "archive.flac"
    archived.write_bytes(b"recorded audio")
    link = tmp_path / "Recording.flac"
    try:
        link.symlink_to(archived)
    except OSError:
        pytest.skip("creating symbolic links needs extra privileges here")
    expected = archived.stat()

    with pytest.raises(ValidationError, match="is a link"):
        pull_module._delete_unchanged_source(link, expected, sha256_file(archived))

    assert archived.read_bytes() == b"recorded audio"
    assert link.is_symlink()


def test_listing_error_on_one_entry_keeps_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audio_transcriber.pull as pull_module

    good = _recording(tmp_path, "Recording.flac")
    odd = _recording(tmp_path, "Recording (2).flac")
    real_scandir = os.scandir

    class Entry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name, self.path, self._entry = entry.name, entry.path, entry

        def is_file(self) -> bool:
            if self.name == odd.name:
                raise PermissionError("simulated unreadable entry")
            return self._entry.is_file()

    class Listing:
        def __init__(self, path: Path) -> None:
            self.inner = real_scandir(path)

        def __enter__(self) -> list[Entry]:
            return [Entry(entry) for entry in self.inner]

        def __exit__(self, *exc: object) -> None:
            self.inner.close()

    monkeypatch.setattr(pull_module.os, "scandir", Listing)
    payload = _pull(tmp_path, _config_file(tmp_path, ARCHIVE))

    names = [Path(item["file"]).name for item in payload["items"]]
    assert names == [good.name, odd.name]
    assert payload["items"][0]["status"] == "transcribed"


def test_cli_pull_does_not_need_an_unused_active_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    _recording(tmp_path)
    config_file = _config_file(tmp_path, ARCHIVE)
    with config_file.open("a", encoding="utf-8") as stream:
        # The built-in azure-ja profile has no endpoint, so it cannot be resolved.
        stream.write("transcription:\n  active:\n    mode: cloud\n    profile: azure-ja\n")

    assert main(["pull", "--config", str(config_file), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert payload["items"][0]["profile"] == "local/local-small"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse tags")
@pytest.mark.parametrize(
    ("tag", "deleted"),
    [(0x9000001A, True), (0xA000000C, False), (0xA0000003, False)],
    ids=["onedrive-cloud-file", "symbolic-link", "junction"],
)
def test_only_link_reparse_tags_block_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: int, deleted: bool
) -> None:
    import audio_transcriber.pull as pull_module

    source = tmp_path / "Recording.flac"
    source.write_bytes(b"recorded audio")

    def fake_tag_info(_handle: int, _class: int, pointer: Any, _size: int) -> bool:
        info = pointer._obj
        info.FileAttributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
        info.ReparseTag = tag
        return True

    monkeypatch.setattr(pull_module, "_get_file_information", fake_tag_info)
    if deleted:
        pull_module._delete_unchanged_source(source, source.stat(), sha256_file(source))
    else:
        with pytest.raises(ValidationError, match="is a link"):
            pull_module._delete_unchanged_source(source, source.stat(), sha256_file(source))
    assert source.exists() is not deleted
