from core.sp1_boundaries import (
    ExternalAdapter,
    OperationResult,
    ReconnectState,
    SmartReconnectBoundary,
)


class FakeReconnect:
    state = ReconnectState.CONNECTED

    def check_connection(self) -> OperationResult:
        return OperationResult(True, "connected")

    def reconnect(self) -> OperationResult:
        self.state = ReconnectState.RECONNECTED
        return OperationResult(True, "reconnected")


class FakeAdapter:
    name = "fake"

    def health_check(self) -> OperationResult:
        return OperationResult(True, "healthy")

    def shutdown(self) -> None:
        return None


def test_runtime_boundary_contracts():
    reconnect = FakeReconnect()
    adapter = FakeAdapter()

    assert isinstance(reconnect, SmartReconnectBoundary)
    assert isinstance(adapter, ExternalAdapter)

    assert reconnect.check_connection().code == "connected"
    assert reconnect.reconnect().success is True
    assert adapter.health_check().success is True


def test_operation_result_is_structured_and_immutable():
    result = OperationResult(
        success=False,
        code="window_unavailable",
        message="Target window could not be read.",
        details={"retryable": True},
    )

    assert result.success is False
    assert result.code == "window_unavailable"
    assert result.details == {"retryable": True}
