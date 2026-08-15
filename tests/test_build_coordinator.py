from pathlib import Path
from xml.etree import ElementTree

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


def test_wgc_native_and_manifest_sources_change_build_identity(
    tmp_path: Path,
) -> None:
    _source(tmp_path)
    inputs = (
        tmp_path / "native" / "helper.cpp",
        tmp_path / "native" / "helper.h",
        tmp_path / "packaging" / "FLASH.exe.manifest",
        tmp_path / "packaging" / "Package.appxmanifest",
    )
    previous = source_digest(tmp_path)
    for path in inputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.suffix + "\n", encoding="utf-8")
        current = source_digest(tmp_path)
        assert current != previous
        previous = current


def test_wgc_build_uses_isolated_helper_and_matching_package_identity() -> None:
    coordinator = Path("scripts/build_coordinator.py").read_text(
        encoding="utf-8"
    )
    spec = Path("FLASH.spec").read_text(encoding="utf-8")
    registration = Path(
        "scripts/register_wgc_validation_package.ps1"
    ).read_text(encoding="utf-8")
    executable_manifest = ElementTree.parse(
        "packaging/wgc-validation/FLASH.exe.manifest"
    ).getroot()
    package_manifest = ElementTree.parse(
        "packaging/wgc-validation/Package.appxmanifest"
    ).getroot()

    assert 'work_dir / "windows_graphics_capture_helper.dll"' in coordinator
    assert 'build_environment["FLASH_WGC_HELPER_DLL"]' in coordinator
    assert "FLASH_WGC_HELPER_DLL" in spec
    assert "windows_graphics_capture_helper.dll" in spec
    assert "packaging/wgc-validation/FLASH.exe.manifest" in spec

    msix = executable_manifest.find(
        "{urn:schemas-microsoft-com:msix.v1}msix"
    )
    assert msix is not None
    identity = package_manifest.find(
        "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}Identity"
    )
    assert identity is not None
    application = package_manifest.find(
        ".//{http://schemas.microsoft.com/appx/manifest/foundation/windows10}Application"
    )
    assert application is not None
    assert msix.attrib == {
        "publisher": identity.attrib["Publisher"],
        "packageName": identity.attrib["Name"],
        "applicationId": application.attrib["Id"],
    }
    assert application.attrib[
        "{http://schemas.microsoft.com/appx/manifest/uap/windows10/10}RuntimeBehavior"
    ] == "win32App"
    assert "EntryPoint" not in application.attrib
    capability_names = {
        capability.attrib["Name"]
        for capability in package_manifest.find(
            "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}Capabilities"
        )
    }
    assert capability_names == {
        "runFullTrust",
        "unvirtualizedResources",
        "graphicsCaptureWithoutBorder",
    }
    assert "MakeAppx pack /d $stageRoot /p $packagePath /o /nv" in (
        registration
    )
    assert "-ValidationOnly" in registration
    assert '[ValidatePattern("^[0-9A-Fa-f]{64}$")]' in registration
    assert "[string]$ExpectedSha256" in registration
    assert "Get-FileHash -LiteralPath $flashExecutable" in registration
    assert registration.index("Get-FileHash") < registration.index(
        "Add-AppxPackage"
    )
    assert "Cert:\\CurrentUser\\TrustedPeople" in registration
    assert "Cert:\\CurrentUser\\Root" in registration
    assert registration.index("Cert:\\CurrentUser\\Root") < (
        registration.index("Add-AppxPackage")
    )
    assert "Add-AppxPackage -Path $packagePath -ExternalLocation" in (
        registration
    )
