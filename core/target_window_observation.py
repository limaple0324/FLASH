"""Player-safe, read-only fact about the configured target game window."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class TargetWindowObservation:
    """A current-process fact that excludes handles and other control details."""

    configured: bool
    safe: bool
    code: str

    def __post_init__(self) -> None:
        if type(self.configured) is not bool:
            raise TypeError("configured must be bool.")
        if type(self.safe) is not bool:
            raise TypeError("safe must be bool.")
        if self.safe and not self.configured:
            raise ValueError("an unconfigured target window cannot be safe.")
        if not isinstance(self.code, str):
            raise TypeError("code must be str.")

        code = self.code.strip()
        if not code:
            raise ValueError("code must not be empty.")
        object.__setattr__(self, "code", code)

    @classmethod
    def not_observed(cls) -> "TargetWindowObservation":
        return cls(
            configured=False,
            safe=False,
            code="window.not_observed",
        )

    @classmethod
    def from_detection(
        cls,
        detection: Mapping[str, object],
    ) -> "TargetWindowObservation":
        """Convert an SP1 detection result without forwarding technical details."""
        if not isinstance(detection, Mapping):
            raise TypeError("detection must be a mapping.")

        configured = detection.get("configured")
        safe = detection.get("safe")
        code = detection.get("code")
        if type(configured) is not bool:
            raise TypeError("detection configured must be bool.")
        if type(safe) is not bool:
            raise TypeError("detection safe must be bool.")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("detection code must be a non-empty string.")

        return cls(
            configured=configured,
            safe=safe,
            code=code.strip(),
        )
