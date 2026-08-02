from core.sp1_boundaries import ExternalAdapter, OperationResult
from core.target_window_observation import TargetWindowObservation
from main import (
    build_services,
    detect_target_window,
    publish_target_window_observation,
    run,
)
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
    assert adapter.health_checks == 1
    assert adapter.input_calls == 0


def test_unconfigured_self_check_run_publishes_a_safe_disabled_observation(
    tmp_path,
) -> None:
    result = run(self_check_only=True, root=tmp_path)

    observation = AppContext.get(TargetWindowStateService).snapshot()

    assert result == 0
    assert observation.configured is False
    assert observation.safe is False
    assert observation.code == "window.not_configured"
