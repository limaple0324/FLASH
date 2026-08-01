from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-windows.yml"


def test_latest_release_requires_manual_approval_and_reuse_check():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "approve_latest:" in workflow
    assert (
        "if: github.event_name == 'workflow_dispatch' && "
        "github.ref == 'refs/heads/main' && inputs.approve_latest"
    ) in workflow
    assert "reject_reused_release_version" in workflow
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" not in workflow


def test_sp1_release_also_requires_manual_approval():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "if: github.event_name == 'workflow_dispatch' && "
        "github.ref == 'refs/heads/sp1/completion-2026-07-25' && inputs.publish_sp1"
    ) in workflow
