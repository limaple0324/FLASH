from pathlib import Path

import pytest

from scripts.build_coordinator import BuildCoordinator, source_digest


def _source(root: Path) -> None:
    (root / "main.py").write_text("print('stable')\n", encoding="utf-8")
    (root / "FLASH.spec").write_text("# spec\n", encoding="utf-8")


def test_generated_outputs_do_not_change_source_identity(tmp_path: Path) -> None:
    _source(tmp_path)
    before = source_digest(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "FLASH.exe").write_bytes(b"old")
    (tmp_path / ".build-cache").mkdir()
    (tmp_path / ".build-cache" / "cache.bin").write_bytes(b"cache")

    assert source_digest(tmp_path) == before


def test_same_source_reuses_verified_cache_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    _source(tmp_path)
    calls = []

    def executor(_root, dist_dir, _work_dir):
        calls.append("build")
        dist_dir.mkdir(parents=True)
        (dist_dir / "FLASH.exe").write_bytes(b"verified")

    coordinator = BuildCoordinator(tmp_path, executor=executor)
    first = coordinator.build()
    second = coordinator.build()

    assert calls == ["build"]
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert (tmp_path / "dist" / "FLASH.exe").read_bytes() == b"verified"


def test_cache_staging_component_stays_short_for_windows_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _source(tmp_path)
    staging_names = []

    def executor(_root, dist_dir, _work_dir):
        dist_dir.mkdir(parents=True)
        (dist_dir / "FLASH.exe").write_bytes(b"verified")

    cache_dir = tmp_path / ".build-cache"
    original_mkdir = Path.mkdir

    def record_staging_name(path, *args, **kwargs):
        if path.parent == cache_dir and path.name.startswith(".tmp-"):
            staging_names.append(path.name)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", record_staging_name)
    BuildCoordinator(tmp_path, executor=executor).build()

    assert len(staging_names) == 1
    assert len(staging_names[0]) == len(".tmp-") + 32
    assert source_digest(tmp_path) not in staging_names[0]


def test_source_change_during_build_keeps_existing_formal_output(
    tmp_path: Path,
) -> None:
    _source(tmp_path)
    output_dir = tmp_path / "dist"
    output_dir.mkdir()
    (output_dir / "FLASH.exe").write_bytes(b"previous")

    def executor(root, dist_dir, _work_dir):
        dist_dir.mkdir(parents=True)
        (dist_dir / "FLASH.exe").write_bytes(b"mixed")
        (root / "main.py").write_text("print('changed')\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="來源已變更"):
        BuildCoordinator(tmp_path, executor=executor).build()

    assert (output_dir / "FLASH.exe").read_bytes() == b"previous"
