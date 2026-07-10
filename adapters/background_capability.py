"""Background read/input capability probes for FLASH SP1.

The probe never performs a real game action. It coordinates adapter-supplied,
explicitly non-destructive checks so FLASH can learn whether a target supports
background capture or background input before those modes are enabled.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Protocol


class CapabilityState(str, Enum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNTESTED = "untested"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    name: str
    state: CapabilityState
    message: str

    @property
    def supported(self) -> bool:
        return self.state is CapabilityState.SUPPORTED


@dataclass(frozen=True, slots=True)
class BackgroundCapabilityReport:
    background_capture: CapabilityResult
    background_input: CapabilityResult
    minimized_input: CapabilityResult

    @property
    def fully_supported(self) -> bool:
        return all(
            item.supported
            for item in (
                self.background_capture,
                self.background_input,
                self.minimized_input,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "fully_supported": self.fully_supported,
            "capabilities": {
                "background_capture": asdict(self.background_capture),
                "background_input": asdict(self.background_input),
                "minimized_input": asdict(self.minimized_input),
            },
        }


class BackgroundCapabilityBackend(Protocol):
    """Game/window-specific, non-destructive capability checks."""

    def probe_background_capture(self, window_handle: int) -> bool | None:
        """Return True, False, or None when capture support cannot be determined."""

    def probe_background_input(self, window_handle: int) -> bool | None:
        """Use only a configured harmless probe; never perform a gameplay action."""

    def probe_minimized_input(self, window_handle: int) -> bool | None:
        """Return whether harmless input is accepted while minimized."""


class BackgroundCapabilityProbe:
    """Run conservative probes and convert them into stable SP1 diagnostics."""

    def __init__(self, backend: BackgroundCapabilityBackend):
        self._backend = backend

    @staticmethod
    def _result(name: str, value: bool | None, *, error: Exception | None = None) -> CapabilityResult:
        if error is not None:
            return CapabilityResult(name, CapabilityState.ERROR, str(error))
        if value is True:
            return CapabilityResult(name, CapabilityState.SUPPORTED, "Capability probe passed.")
        if value is False:
            return CapabilityResult(name, CapabilityState.UNSUPPORTED, "Capability probe was rejected.")
        return CapabilityResult(name, CapabilityState.UNKNOWN, "Capability could not be determined safely.")

    def _run(self, name: str, callback, window_handle: int) -> CapabilityResult:
        try:
            return self._result(name, callback(window_handle))
        except Exception as exc:  # probes report failure instead of aborting startup
            return self._result(name, None, error=exc)

    def run(self, window_handle: int | None) -> BackgroundCapabilityReport:
        if not window_handle:
            untested = lambda name: CapabilityResult(
                name,
                CapabilityState.UNTESTED,
                "No target window is selected; capability probe was not run.",
            )
            return BackgroundCapabilityReport(
                background_capture=untested("background_capture"),
                background_input=untested("background_input"),
                minimized_input=untested("minimized_input"),
            )

        return BackgroundCapabilityReport(
            background_capture=self._run(
                "background_capture", self._backend.probe_background_capture, window_handle
            ),
            background_input=self._run(
                "background_input", self._backend.probe_background_input, window_handle
            ),
            minimized_input=self._run(
                "minimized_input", self._backend.probe_minimized_input, window_handle
            ),
        )
