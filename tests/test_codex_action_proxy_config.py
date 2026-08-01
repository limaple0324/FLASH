from pathlib import Path


def test_codex_action_preserves_proxy_authentication_config() -> None:
    workflow = Path(".github/workflows/codex-queue-runner.yml").read_text(encoding="utf-8")
    agent = workflow.split("  validate:", 1)[0].split("  agent:", 1)[1]

    assert "uses: openai/codex-action@52fe01ec70a42f454c9d2ebd47598f9fd6893d56" in agent
    assert 'codex-version: "0.146.0"' in agent
    assert "safety-strategy: drop-sudo" in agent
    assert "codex-args: '[\"--ephemeral\"]'" in agent
    assert "--ignore-user-config" not in agent
