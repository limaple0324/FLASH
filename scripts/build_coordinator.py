"""Serialize local Windows builds and publish only stable isolated results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".build-cache",
        ".git",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        "release",
    }
)
_BUILD_INPUT_SUFFIXES = frozenset(
    {
        ".bat",
        ".bmp",
        ".cmd",
        ".cpp",
        ".gif",
        ".h",
        ".ico",
        ".jpeg",
        ".jpg",
        ".json",
        ".manifest",
        ".appxmanifest",
        ".png",
        ".ps1",
        ".py",
        ".spec",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
        ".yaml",
        ".yml",
    }
)


BuildExecutor = Callable[[Path, Path, Path], None]


@dataclass(frozen=True, slots=True)
class BuildResult:
    source_digest: str
    output_path: Path
    cache_hit: bool


class _BuildLock:
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._handle = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("等待其他編譯完成逾時。")
                time.sleep(0.2)
        self._handle = handle
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def source_digest(root: Path) -> str:
    """Hash stable build inputs while ignoring all generated outputs."""
    root = Path(root).resolve()
    digest = hashlib.sha256()
    candidates = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if any(part in _EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if candidate.suffix.casefold() not in _BUILD_INPUT_SUFFIXES:
            continue
        candidates.append((relative, candidate))
    for relative, candidate in sorted(candidates):
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with candidate.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _default_executor(root: Path, dist_dir: Path, work_dir: Path) -> None:
    helper_path = work_dir / "windows_graphics_capture_helper.dll"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "build_wgc_helper.py"),
            "--root",
            str(root),
            "--output",
            str(helper_path),
        ],
        cwd=root,
        check=True,
    )
    build_environment = os.environ.copy()
    build_environment["FLASH_WGC_HELPER_DLL"] = str(helper_path)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(work_dir),
            str(root / "FLASH.spec"),
        ],
        cwd=root,
        env=build_environment,
        check=True,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class BuildCoordinator:
    def __init__(
        self,
        root: Path,
        *,
        output_dir: Path | None = None,
        cache_dir: Path | None = None,
        lock_path: Path | None = None,
        timeout_seconds: float = 1800,
        executor: BuildExecutor | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.output_dir = (
            Path(output_dir).resolve()
            if output_dir is not None
            else self.root / "dist"
        )
        self.cache_dir = (
            Path(cache_dir).resolve()
            if cache_dir is not None
            else self.root / ".build-cache"
        )
        self.lock_path = (
            Path(lock_path).resolve()
            if lock_path is not None
            else self.root / ".flash-build.lock"
        )
        self.timeout_seconds = timeout_seconds
        self.executor = executor or _default_executor

    @staticmethod
    def _valid_cache(entry: Path, digest: str) -> Path | None:
        executable = entry / "FLASH.exe"
        manifest_path = entry / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not executable.is_file()
            or manifest.get("source_digest") != digest
            or manifest.get("sha256") != _file_sha256(executable)
        ):
            return None
        return executable

    def _save_cache(self, executable: Path, digest: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = self.cache_dir / digest
        cached = self._valid_cache(entry, digest)
        if cached is not None:
            return cached
        temporary = self.cache_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir(parents=True)
        try:
            target = temporary / "FLASH.exe"
            shutil.copy2(executable, target)
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "source_digest": digest,
                        "sha256": _file_sha256(target),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if entry.exists():
                shutil.rmtree(entry)
            os.replace(temporary, entry)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return entry / "FLASH.exe"

    def _publish(self, executable: Path) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.output_dir / f".FLASH.{uuid.uuid4().hex}.tmp"
        final_path = self.output_dir / "FLASH.exe"
        try:
            shutil.copy2(executable, temporary)
            os.replace(temporary, final_path)
        finally:
            temporary.unlink(missing_ok=True)
        return final_path

    def build(self) -> BuildResult:
        with _BuildLock(self.lock_path, self.timeout_seconds):
            before = source_digest(self.root)
            entry = self.cache_dir / before
            executable = self._valid_cache(entry, before)
            cache_hit = executable is not None
            if executable is None:
                with tempfile.TemporaryDirectory(
                    prefix="flash-isolated-build-"
                ) as temporary_root:
                    isolated = Path(temporary_root)
                    isolated_dist = isolated / "dist"
                    isolated_work = isolated / "work"
                    self.executor(self.root, isolated_dist, isolated_work)
                    built = isolated_dist / "FLASH.exe"
                    if not built.is_file():
                        raise FileNotFoundError(
                            "隔離編譯沒有產生 FLASH.exe。"
                        )
                    after = source_digest(self.root)
                    if after != before:
                        raise RuntimeError(
                            "編譯期間來源已變更，本次成品不發布。"
                        )
                    executable = self._save_cache(built, before)
            if source_digest(self.root) != before:
                raise RuntimeError("發布前來源已變更，本次成品不發布。")
            output_path = self._publish(executable)
            return BuildResult(before, output_path, cache_hit)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = BuildCoordinator(
        args.root,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        lock_path=args.lock_file,
        timeout_seconds=args.timeout_seconds,
    ).build()
    print(
        json.dumps(
            {
                "source_digest": result.source_digest,
                "output_path": str(result.output_path),
                "cache_hit": result.cache_hit,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
