from pathlib import Path

import pytest

from audio_transcriber import gpu_runtime
from audio_transcriber.errors import ValidationError


def _create_required_dlls(directory: Path) -> None:
    for name in gpu_runtime.WINDOWS_CUDA_DLLS:
        (directory / name).write_bytes(b"test dll")


def test_cpu_device_does_not_check_windows_dlls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gpu_runtime, "_is_windows", lambda: True)
    monkeypatch.setenv("PATH", "")

    gpu_runtime.validate_cuda_runtime("cpu")


def test_cuda_device_check_is_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_runtime, "_is_windows", lambda: False)
    monkeypatch.setenv("PATH", "")

    gpu_runtime.validate_cuda_runtime("cuda")


def test_cuda_device_reports_all_missing_dlls_before_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gpu_runtime, "_is_windows", lambda: True)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ValidationError) as raised:
        gpu_runtime.validate_cuda_runtime("cuda")

    message = str(raised.value)
    assert "before audio processing" in message
    assert "cublas64_12.dll" in message
    assert "cudnn_ops64_9.dll" in message
    assert "where.exe cublas64_12.dll" in message
    assert "CPU profiles do not require these DLLs" in message


def test_cuda_device_loads_every_required_dll_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _create_required_dlls(second)
    loaded: list[Path] = []
    monkeypatch.setattr(gpu_runtime, "_is_windows", lambda: True)
    monkeypatch.setenv("PATH", f'"{first}";"{second}"')
    monkeypatch.setattr(gpu_runtime, "_load_windows_dll", loaded.append)

    gpu_runtime.validate_cuda_runtime("CUDA")

    assert [path.name for path in loaded] == list(gpu_runtime.WINDOWS_CUDA_DLLS)
    assert all(path.parent == second for path in loaded)


def test_cuda_device_reports_dll_that_exists_but_cannot_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_required_dlls(tmp_path)
    monkeypatch.setattr(gpu_runtime, "_is_windows", lambda: True)
    monkeypatch.setenv("PATH", str(tmp_path))

    def reject_cublas(path: Path) -> object:
        if path.name == "cublas64_12.dll":
            raise OSError("dependent DLL is missing")
        return object()

    monkeypatch.setattr(gpu_runtime, "_load_windows_dll", reject_cublas)

    with pytest.raises(ValidationError, match="dependent DLL is missing") as raised:
        gpu_runtime.validate_cuda_runtime("cuda")

    assert "cublas64_12.dll was found" in str(raised.value)
