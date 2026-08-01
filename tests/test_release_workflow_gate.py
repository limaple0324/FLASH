from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
VALIDATION_WORKFLOW = WORKFLOWS / "build-windows.yml"
FORMAL_WORKFLOW = WORKFLOWS / "publish-windows-release.yml"


def test_validation_workflow_cannot_publish_a_formal_channel():
    workflow = VALIDATION_WORKFLOW.read_text(encoding="utf-8")

    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "contents: write" not in workflow
    assert "git push origin release/" not in workflow


def test_formal_release_requires_manual_main_approval_and_history_check():
    workflow = FORMAL_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.actor == 'limaple0324'" in workflow
    assert "inputs.confirm_release" in workflow
    assert "reject_reused_release_history_version" in workflow
