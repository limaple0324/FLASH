from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/build-windows.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_windows_build_reads_product_and_technical_identity_as_json():
    workflow = _workflow()

    assert (
        "from core.version import PRODUCT_NAME, TECHNICAL_NAME, "
        "VERSION, MILESTONE"
    ) in workflow
    assert "json.dumps(" in workflow
    assert "$metadataJson | ConvertFrom-Json" in workflow
    assert "FLASH_PRODUCT_NAME=$($metadata.product)" in workflow
    assert "FLASH_TECHNICAL_NAME=$($metadata.technical_name)" in workflow


def test_windows_build_info_keeps_product_and_technical_names_separate():
    workflow = _workflow()

    assert '"product=$env:FLASH_PRODUCT_NAME"' in workflow
    assert '"technical_name=$env:FLASH_TECHNICAL_NAME"' in workflow
    assert '"version=$env:FLASH_VERSION"' in workflow
    assert '"milestone=$env:FLASH_MILESTONE"' in workflow


def test_validation_build_has_no_formal_release_channel():
    workflow = _workflow()

    assert "$publishTarget -ne 'none'" in workflow
    assert "$artifactKind -ne 'validation'" in workflow
    assert "$approvalStatus -ne 'not_approved'" in workflow
    assert "$approvalMethod -ne 'none'" in workflow
    assert "The Windows build workflow must produce a validation-only artifact." in workflow
    assert "分支驗證說明.txt" in workflow
    assert "release/latest" not in workflow
    assert "release/sp1" not in workflow


def test_workflow_uses_the_coordinated_build_output():
    workflow = _workflow()

    assert (
        "python scripts/build_coordinator.py --root . "
        "--output-dir dist --cache-dir .build-cache"
    ) in workflow
    assert "verify_windows_release.ps1' -NoLaunch" in workflow
