from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from audio_transcriber.cli import main
from audio_transcriber.errors import ConfigError
from audio_transcriber.output import emit, lines, public_result


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


COMMANDS = [
    ["config", "list"],
    ["config", "profiles"],
    ["config", "init", "--dry-run"],
    ["config", "migrate", "config.yaml", "--dry-run"],
    ["model", "download", "small", "--dry-run"],
    ["transcribe", "audio.wav", "--dry-run"],
    ["transcribe", "audio.wav", "--suggest-phrases", "--dry-run"],
    ["suggest-phrases", "transcript.jsonl", "--output-dir", "review", "--dry-run"],
    ["validate", "manifest.json"],
    ["cleanup"],
    ["status"],
]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("json_output", [False, True])
def test_all_commands_share_output_contract(
    command: list[str],
    json_output: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The adapters are replaced so no processing, authentication or AI occurs.
    import audio_transcriber.cli as cli
    from audio_transcriber.models import OutputPaths, TranscriptionResult

    payload = {"status": "valid", "count": 1, "outputs": {"path": str(tmp_path / "音声 file.txt")}}

    class Config:
        config_sources: list[dict[str, Any]] = []
        values = {"ffmpeg_executable": "ffmpeg", "subprocess_timeout_seconds": 60}

        def display(self) -> dict[str, Any]:
            return payload

        def profiles_display(self) -> dict[str, Any]:
            return payload

        def path(self, key: str) -> Path:
            return tmp_path / key

    monkeypatch.setattr(cli, "_resolve", lambda *a, **kw: Config())
    for name in (
        "initialize_user_config",
        "migrate_config_file",
        "download_model",
        "validate_artifact",
        "cleanup_jobs",
        "list_jobs",
    ):
        monkeypatch.setattr(cli, name, lambda *a, **kw: payload)
    monkeypatch.setattr("audio_transcriber.prepare.prepare_transcription", lambda *a, **kw: payload)
    monkeypatch.setattr(
        "audio_transcriber.phrase_suggestions.suggest_phrases", lambda *a, **kw: payload
    )
    paths = OutputPaths(
        *(tmp_path / name for name in ("a.flac", "a.md", "a.srt", "a.jsonl", "a.json"))
    )
    transcription = TranscriptionResult("planned", tmp_path / "audio.wav", "abc", paths, 0)
    monkeypatch.setattr(cli.TranscriptionPipeline, "transcribe", lambda *a, **kw: transcription)
    expected = (
        transcription.to_dict()
        if command[0] == "transcribe" and "--suggest-phrases" not in command
        else payload
    )
    assert main([*command, *(["--json"] if json_output else [])]) == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out
    assert (
        json.loads(captured.out) == expected
        if json_output
        else captured.out.splitlines() == lines(expected)
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        ["--json", "config", "list"],
        ["config", "--json", "list"],
        ["config", "list", "--json"],
        ["--json", "config", "list", "--json"],
        ["model", "--json", "download", "small", "--dry-run"],
    ],
)
def test_json_is_retained_at_every_parser_level(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(args) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)


@pytest.mark.parametrize(
    "args",
    [
        ["--json", "invalid"],
        ["config", "--json", "invalid"],
        ["transcribe", "a.wav", "--chunk-seconds", "bad", "--json"],
        ["validate", "--json"],
        ["--json=true", "status"],
        ["--json", "--quiet", "config", "list", "--verbose"],
        ["status", "--unknown", "--json"],
    ],
)
def test_argument_errors_are_one_json_document(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(args) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "failed" and result["exit_code"] == 2
    assert result["error"]["type"] == "UsageError"
    assert "[ERROR]" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "error,code,status",
    [
        (ConfigError("invalid config"), 2, "failed"),
        (RuntimeError("test runtime error"), 1, "failed"),
        (KeyboardInterrupt(), 130, "interrupted"),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("verbosity", [[], ["--quiet"], ["--verbose"]])
def test_failure_and_interrupt_formats(
    error: BaseException,
    code: int,
    status: str,
    json_output: bool,
    verbosity: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr("audio_transcriber.cli.list_jobs", fail)
    assert (
        main(
            [
                "status",
                "--state-dir",
                str(tmp_path / "state"),
                *verbosity,
                *(["--json"] if json_output else []),
            ]
        )
        == code
    )
    captured = capsys.readouterr()
    if json_output:
        result = json.loads(captured.out)
        assert result["status"] == status
        assert result["exit_code"] == code
        assert result["target_paths"] == [str(tmp_path / "state")]
    else:
        assert f"status={status}" in captured.out.splitlines()
        assert f"target_paths[0]={tmp_path / 'state'}" in captured.out.splitlines()
    assert "[ERROR]" in captured.err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("verbosity", [[], ["--quiet"], ["--verbose"]])
@pytest.mark.parametrize("json_output", [False, True])
def test_actual_dry_run_no_writes(
    verbosity: list[str],
    json_output: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "音声 recording.wav"
    source.write_bytes(b"dry run fixture")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (
        main(
            [
                "transcribe",
                str(source),
                "--dry-run",
                *verbosity,
                *(["--json"] if json_output else []),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert (
        json.loads(captured.out)["plan"]["network_calls"] == 0
        if json_output
        else "plan.network_calls=0" in captured.out.splitlines()
    )
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert set(tmp_path.iterdir()) == {source}
    assert bool(captured.err) is ("--quiet" not in verbosity)


def test_full_details_and_cleanup_failures_preserve_api_data() -> None:
    original = {
        "status": "applied",
        "state_dir": r"C:\path\to\state",
        "candidates": [],
        "skipped": [{"job_id": "a", "reason": "not completed"}],
        "failures": [{"job_id": "b", "reason": "access denied"}],
    }
    rendered = public_result(original)
    assert rendered["failures_count"] == rendered["skipped_count"] == 1
    assert rendered["failures"][0]["job_directory"] == r"C:\path\to\state\jobs\b"
    assert "job_directory" not in original["failures"][0]
    assert "failures_count" not in original
    many = {"jobs": [{"job_directory": f"job-{i}"} for i in range(1500)]}
    assert len(lines(many)) == 1500


@pytest.mark.parametrize("tty", [False, True])
def test_copyable_long_paths_do_not_depend_on_tty(
    tty: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Buffer(io.StringIO):
        def isatty(self) -> bool:
            return tty

    buffer = Buffer()
    monkeypatch.setattr(sys, "stdout", buffer)
    path = "C:\\" + "long folder\\" * 40 + "音声.wav"
    emit({"path": path, "line\nkey": "a\rb\nc\t\x1b"}, json_output=False)
    assert buffer.getvalue().splitlines() == [f"path={path}", r"line\nkey=a\rb\nc\t\u001b"]


@pytest.mark.parametrize("json_output", [False, True])
def test_real_entrypoint_pipe_and_redirect(json_output: bool, tmp_path: Path) -> None:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    command = [
        sys.executable,
        "-m",
        "audio_transcriber",
        "status",
        "--state-dir",
        str(tmp_path / "state"),
        *(["--json"] if json_output else []),
    ]
    piped = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", env=env, check=True
    )
    destination = tmp_path / "redirect.txt"
    with destination.open("w", encoding="utf-8") as output:
        subprocess.run(command, stdout=output, stderr=subprocess.PIPE, env=env, check=True)
    assert destination.read_text(encoding="utf-8") == piped.stdout
    assert "\x1b" not in piped.stdout
    assert (
        json.loads(piped.stdout)["job_count"] == 0 if json_output else "job_count=0" in piped.stdout
    )


@pytest.mark.parametrize("json_output", [False, True])
def test_validation_exposes_failing_output_path_separately(
    json_output: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing output.md"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outputs": {"markdown": {"path": str(missing), "sha256": "abc", "size": 1}},
            }
        ),
        encoding="utf-8",
    )
    assert main(["validate", str(manifest), *(["--json"] if json_output else [])]) == 2
    captured = capsys.readouterr()
    if json_output:
        assert json.loads(captured.out)["target_paths"] == [str(manifest), str(missing)]
    else:
        assert f"target_paths[1]={missing}" in captured.out.splitlines()


def test_cleanup_partial_failure_keeps_existing_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = {
        "status": "applied",
        "state_dir": str(tmp_path),
        "candidates": [],
        "skipped": [],
        "failures": [{"job_id": "a", "reason": "access denied"}],
    }
    monkeypatch.setattr("audio_transcriber.cli.cleanup_jobs", lambda *a, **kw: result)
    assert main(["cleanup", "--apply", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["failures_count"] == 1
    assert rendered["failures"][0]["job_directory"] == str(tmp_path / "jobs" / "a")
    assert result["failures"] == [{"job_id": "a", "reason": "access denied"}]
