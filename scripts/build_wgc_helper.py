"""Build the validation-only Windows Graphics Capture helper with MSVC."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _visual_studio_environment() -> Path | None:
    program_files = os.environ.get("ProgramFiles(x86)")
    if not program_files:
        return None
    vswhere = (
        Path(program_files)
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )
    if not vswhere.is_file():
        return None
    result = subprocess.run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    installation = result.stdout.strip()
    if not installation:
        return None
    candidate = (
        Path(installation)
        / "VC"
        / "Auxiliary"
        / "Build"
        / "vcvarsall.bat"
    )
    return candidate if candidate.is_file() else None


def build(root: Path, output: Path) -> Path:
    if os.name != "nt":
        raise RuntimeError("WGC 協助程式只能在 Windows 使用 MSVC 編譯。")
    root = Path(root).resolve()
    output = Path(output).resolve()
    source = root / "native" / "windows_graphics_capture_helper.cpp"
    if not source.is_file():
        raise FileNotFoundError(source)

    with tempfile.TemporaryDirectory(prefix="flash-wgc-helper-") as raw_temp:
        temporary = Path(raw_temp)
        built = temporary / "windows_graphics_capture_helper.dll"
        object_path = temporary / "windows_graphics_capture_helper.obj"
        arguments = [
            "cl.exe",
            "/nologo",
            "/std:c++20",
            "/EHsc",
            "/O2",
            "/MD",
            "/LD",
            "/permissive-",
            "/DUNICODE",
            "/D_UNICODE",
            f"/Fo{object_path}",
            f"/Fe{built}",
            str(source),
            "/link",
            "d3d11.lib",
            "user32.lib",
            "windowsapp.lib",
        ]
        if shutil.which("cl.exe"):
            subprocess.run(arguments, cwd=temporary, check=True)
        else:
            vcvarsall = _visual_studio_environment()
            if vcvarsall is None:
                raise RuntimeError(
                    "找不到具備 C++ 桌面工具的 Visual Studio Build Tools。"
                )
            compile_command = subprocess.list2cmdline(arguments)
            subprocess.run(
                (
                    'cmd.exe /d /c ""'
                    + str(vcvarsall)
                    + '" x64 >nul && '
                    + compile_command
                    + '"'
                ),
                cwd=temporary,
                check=True,
            )
        if not built.is_file() or built.stat().st_size <= 0:
            raise RuntimeError("MSVC 未產生 WGC 協助程式。")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        shutil.copy2(built, temporary_output)
        os.replace(temporary_output, output)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = args.root.resolve()
    output = args.output or (
        root / "native" / "windows_graphics_capture_helper.dll"
    )
    print(build(root, output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
