from core.sp1_boundaries import ExternalAdapter, OperationResult
from core.target_window_observation import TargetWindowObservation
from main import (
    build_services,
    detect_target_window,
    player_home_status,
    publish_target_window_observation,
    run,
)
from presentation.target_window_status import target_window_player_message
from services.app_context import AppContext
from services.target_window_state_service import TargetWindowStateService


class _ReadOnlyAdapter:
    def __init__(self, result: OperationResult) -> None:
        self.result = result
        self.health_checks = 0
        self.shutdowns = 0
        self.input_calls = 0

    @property
    def name(self) -> str:
        return "read_only_target"

    def health_check(self) -> OperationResult:
        self.health_checks += 1
        return self.result

    def shutdown(self) -> None:
        self.shutdowns += 1

    def send_input(self) -> None:
        self.input_calls += 1
        raise AssertionError("target-window observation must never send input")


def test_real_adapter_health_fact_flows_through_event_bus_without_details(
    tmp_path,
) -> None:
    build_services(root=tmp_path)
    adapter = _ReadOnlyAdapter(
        OperationResult(
            success=True,
            code="window.ready",
            message="raw adapter message",
            details={
                "title": "Private Game Window",
                "handle": 987654,
                "process_id": 321,
            },
        )
    )
    AppContext.register(ExternalAdapter, adapter)

    detection = detect_target_window()
    observation = publish_target_window_observation(detection)
    service = AppContext.get(TargetWindowStateService)

    assert adapter.health_checks == 1
    assert adapter.input_calls == 0
    assert service.snapshot() is observation
    assert observation == TargetWindowObservation(
        configured=True,
        safe=True,
        code="window.ready",
    )
    assert not hasattr(observation, "details")
    player_message = target_window_player_message(observation)
    assert "987654" not in player_message
    assert "Private Game Window" not in player_message


def test_rejected_adapter_fact_stays_unsafe_across_the_event_chain(
    tmp_path,
) -> None:
    build_services(root=tmp_path)
    adapter = _ReadOnlyAdapter(
        OperationResult(
            success=False,
            code="window.ambiguous",
            message="Two matching windows",
            details={"count": 2, "handles": (11, 22)},
        )
    )
    AppContext.register(ExternalAdapter, adapter)

    detection = detect_target_window()
    publish_target_window_observation(detection)
    observation = AppContext.get(TargetWindowStateService).snapshot()

    assert detection["safe"] is False
    assert observation.safe is False
    assert observation.code == "window.ambiguous"
    assert "找到多個" in target_window_player_message(observation)
    assert adapter.health_checks == 1
    assert adapter.input_calls == 0


def test_unconfigured_self_check_run_publishes_a_safe_disabled_observation(
    tmp_path,
) -> None:
    result = run(
        self_check_only=True,
        root=tmp_path,
        card_preview_catalog=None,
    )

    observation = AppContext.get(TargetWindowStateService).snapshot()

    assert result == 0
    assert observation.configured is False
    assert observation.safe is False
    assert observation.code == "window.not_configured"
    assert "操作保持停用" in target_window_player_message(observation)


def test_player_home_status_removes_all_window_control_details() -> None:
    home_status = player_home_status(
        {
            "self_check_passed": True,
            "self_check": [{"message": r"C:\private\self-check.txt"}],
            "window_registry": {
                "loaded": True,
                "backup": r"C:\private\window_registry.corrupt",
                "characters": [
                    {
                        "character_id": "private-character-id",
                        "display_name": "小古",
                        "group": "十四支",
                        "role": "主號",
                        "note": "玩家備註",
                        "aliases": ["舊名稱"],
                        "handle": 987654,
                        "process_id": 321,
                        "window_class": "PrivateClass",
                        "rect": (0, 0, 800, 600),
                        "health": "ready",
                    }
                ],
            },
            "target_window": {
                "configured": True,
                "safe": True,
                "code": "window.ready",
                "message": "raw adapter message",
                "details": {
                    "title": "Private Game Window",
                    "handle": 987654,
                    "rect": (0, 0, 800, 600),
                },
            },
            "background_capabilities": {
                "details": {"handle": 987654},
            },
        }
    )

    assert home_status == {
        "self_check_passed": True,
        "window_registry": {
            "characters": [
                {
                    "display_name": "小古",
                    "group": "十四支",
                    "role": "主號",
                    "note": "玩家備註",
                }
            ]
        },
        "target_window": {
            "configured": True,
            "safe": True,
        },
    }
    rendered = repr(home_status)
    assert "private-character-id" not in rendered
    assert "987654" not in rendered
    assert "Private" not in rendered
    assert "window.ready" not in rendered


def test_player_home_status_does_not_treat_truthy_untrusted_values_as_safe() -> None:
    home_status = player_home_status(
        {
            "self_check_passed": "yes",
            "target_window": {
                "configured": "yes",
                "safe": 1,
            },
        }
    )

    assert home_status["self_check_passed"] is False
    assert home_status["target_window"] == {
        "configured": False,
        "safe": False,
    }
