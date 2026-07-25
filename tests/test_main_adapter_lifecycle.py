from __future__ import annotations

from types import SimpleNamespace

import pytest

import main as main_module
from core.sp1_boundaries import ExternalAdapter, OperationResult
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from services.app_context import AppContext


class RecordingAdapter:
    def __init__(self, *, shutdown_error: Exception | None = None):
        self.shutdown_calls = 0
        self._shutdown_error = shutdown_error

    @property
    def name(self) -> str:
        return "recording_adapter"

    def health_check(self) -> OperationResult:
        return OperationResult(
            success=True,
            code="window.ready",
            message="Target identified without enabling input.",
        )

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._shutdown_error is not None:
            raise self._shutdown_error


class RecordingLogger:
    def __init__(self):
        self.info_messages: list[str] = []
        self.error_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def error(self, message: str) -> None:
        self.error_messages.append(message)


@pytest.fixture(autouse=True)
def clear_app_context():
    AppContext.clear()
    yield
    AppContext.clear()


def prepare_run(monkeypatch, tmp_path, adapter, *, startup_error: Exception | None = None):
    paths = main_module.PathManager(root=tmp_path)
    logger = RecordingLogger()
    registry = WindowRegistry()
    store = WindowRegistryStore(paths.data_dir() / main_module.REGISTRY_FILENAME)

    AppContext.register(WindowRegistryStore, store)
    AppContext.register(WindowRegistry, registry)
    AppContext.register(ExternalAdapter, adapter)

    monkeypatch.setattr(main_module, "build_services", lambda root=None: (paths, logger))

    def start():
        if startup_error is not None:
            raise startup_error
        return {"self_check_passed": True, "self_check": []}

    monkeypatch.setattr(
        main_module,
        "Bootstrap",
        lambda context: SimpleNamespace(start=start),
    )
    return logger


def test_run_shuts_down_adapter_after_self_check_exit(monkeypatch, tmp_path):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)

    assert main_module.run(self_check_only=True, root=tmp_path) == 0
    assert adapter.shutdown_calls == 1


def test_run_shuts_down_adapter_after_normal_window_close(monkeypatch, tmp_path):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)
    window = SimpleNamespace(mainloop=lambda: None)
    monkeypatch.setattr(main_module, "create_main_window", lambda status, paths: window)

    assert main_module.run(root=tmp_path) == 0
    assert adapter.shutdown_calls == 1


def test_run_shuts_down_adapter_after_startup_failure(monkeypatch, tmp_path):
    adapter = RecordingAdapter()
    logger = prepare_run(
        monkeypatch,
        tmp_path,
        adapter,
        startup_error=RuntimeError("original startup failure"),
    )

    assert main_module.run(self_check_only=True, root=tmp_path) == 1
    assert adapter.shutdown_calls == 1
    assert any("original startup failure" in message for message in logger.error_messages)


def test_shutdown_failure_does_not_replace_successful_exit(monkeypatch, tmp_path):
    adapter = RecordingAdapter(shutdown_error=RuntimeError("cleanup failure"))
    logger = prepare_run(monkeypatch, tmp_path, adapter)

    assert main_module.run(self_check_only=True, root=tmp_path) == 0
    assert adapter.shutdown_calls == 1
    assert any("cleanup failure" in message for message in logger.error_messages)


def test_shutdown_failure_does_not_hide_startup_failure(monkeypatch, tmp_path):
    adapter = RecordingAdapter(shutdown_error=RuntimeError("cleanup failure"))
    logger = prepare_run(
        monkeypatch,
        tmp_path,
        adapter,
        startup_error=RuntimeError("original startup failure"),
    )

    assert main_module.run(self_check_only=True, root=tmp_path) == 1
    assert adapter.shutdown_calls == 1
    assert any("original startup failure" in message for message in logger.error_messages)
    assert any("cleanup failure" in message for message in logger.error_messages)
