from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from .errors import ValidationError

WINDOWS_CUDA_DLLS = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn_ops64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_adv64_9.dll",
    "cudnn_graph64_9.dll",
)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_path_directories() -> tuple[Path, ...]:
    directories: list[Path] = []
    for raw_value in os.environ.get("PATH", "").split(";"):
        value = os.path.expandvars(raw_value.strip().strip('"'))
        if value:
            directories.append(Path(value))
    return tuple(directories)


def _find_dll(name: str, directories: tuple[Path, ...]) -> Path | None:
    for directory in directories:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _load_windows_dll(path: Path) -> object:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows DLL loading is unavailable")
    return loader(str(path))


def _installation_guidance() -> str:
    return (
        "Install CUDA Toolkit 12 (cuBLAS) and cuDNN 9 for CUDA 12, add their "
        "DLL directories to PATH, open a new terminal, and verify with "
        "`where.exe cublas64_12.dll` and `where.exe cudnn_ops64_9.dll`. "
        "CPU profiles do not require these DLLs."
    )


def validate_cuda_runtime(device: str) -> None:
    """Validate Windows CUDA DLL availability before costly media processing."""
    if device.casefold() != "cuda" or not _is_windows():
        return

    directories = _windows_path_directories()
    resolved = {name: _find_dll(name, directories) for name in WINDOWS_CUDA_DLLS}
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        raise ValidationError(
            "CUDA GPU runtime preflight failed before audio processing. "
            f"Required DLLs are not available on PATH: {', '.join(missing)}. "
            f"{_installation_guidance()}"
        )

    loaded: list[object] = []
    for name, path in resolved.items():
        assert path is not None
        try:
            loaded.append(_load_windows_dll(path))
        except OSError as exc:
            raise ValidationError(
                "CUDA GPU runtime preflight failed before audio processing. "
                f"{name} was found at {path} but could not be loaded: {exc}. "
                f"{_installation_guidance()}"
            ) from exc
