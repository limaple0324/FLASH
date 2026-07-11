"""Conservative character-to-window rebinding for FLASH SP1.

The engine scores current window observations against an existing registry
record. It never guesses between close candidates and never creates a new
character identity automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from adapters.windows_window import WindowInfo
from core.window_registry import CharacterWindowRecord, WindowHealth, WindowRegistry


@dataclass(frozen=True, slots=True)
class WindowCandidate:
    handle: int
    rect: tuple[int, int, int, int]
    process_id: int | None = None
    window_class: str | None = None
    title: str = ""


@dataclass(frozen=True, slots=True)
class BindingDecision:
    character_id: str
    bound: bool
    confidence: float
    code: str
    message: str
    candidate_handle: int | None = None


class CharacterBindingEngine:
    """Rebind a known character only when one candidate is clearly trustworthy."""

    def __init__(self, registry: WindowRegistry, *, threshold: float = 0.80, margin: float = 0.15):
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be within (0, 1].")
        if not 0.0 <= margin <= 1.0:
            raise ValueError("margin must be within [0, 1].")
        self._registry = registry
        self._threshold = threshold
        self._margin = margin

    @staticmethod
    def _rect_similarity(
        previous: tuple[int, int, int, int] | None,
        current: tuple[int, int, int, int],
    ) -> float:
        if previous is None:
            return 0.0
        pl, pt, pr, pb = previous
        cl, ct, cr, cb = current
        previous_width = max(1, pr - pl)
        previous_height = max(1, pb - pt)
        current_width = max(1, cr - cl)
        current_height = max(1, cb - ct)
        size_delta = abs(previous_width - current_width) / previous_width
        size_delta += abs(previous_height - current_height) / previous_height
        position_delta = abs(pl - cl) / previous_width
        position_delta += abs(pt - ct) / previous_height
        penalty = min(1.0, (size_delta + position_delta) / 4.0)
        return 1.0 - penalty

    @staticmethod
    def _has_persisted_identity(record: CharacterWindowRecord) -> bool:
        """Return whether the record has enough history for automatic rebinding.

        A PID alone is deliberately insufficient because Windows may reuse PIDs
        after the previous process exits or after a reboot.
        """
        return bool(record.window_class and record.rect is not None)

    @classmethod
    def score(cls, record: CharacterWindowRecord, candidate: WindowCandidate) -> float:
        """Return fixed-weight confidence without normalizing weak evidence.

        Fixed weights prevent a single matching PID from becoming 100% confidence.
        Persisted history without a live handle can still reach the default 0.80
        threshold through matching class and geometry.
        """
        score = 0.0

        if record.handle is not None and record.handle == candidate.handle:
            score += 0.45

        if (
            record.process_id is not None
            and candidate.process_id is not None
            and record.process_id == candidate.process_id
        ):
            score += 0.10

        if (
            record.window_class
            and candidate.window_class
            and record.window_class.casefold() == candidate.window_class.casefold()
        ):
            score += 0.35

        if record.rect is not None:
            score += 0.45 * cls._rect_similarity(record.rect, candidate.rect)

        return min(1.0, score)

    def bind(self, character_id: str, candidates: Iterable[WindowCandidate]) -> BindingDecision:
        record = self._registry.get(character_id)
        if not record.confirmed and not self._has_persisted_identity(record):
            return BindingDecision(
                character_id=character_id,
                bound=False,
                confidence=0.0,
                code="binding.unconfirmed_character",
                message="Character has no confirmed window history; explicit player binding is required.",
            )

        ranked = sorted(
            ((self.score(record, candidate), candidate) for candidate in candidates),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked:
            self._registry.mark_offline(character_id)
            return BindingDecision(
                character_id=character_id,
                bound=False,
                confidence=0.0,
                code="binding.no_candidates",
                message="No candidate windows are available.",
            )

        best_score, best = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score < self._threshold:
            return BindingDecision(
                character_id=character_id,
                bound=False,
                confidence=best_score,
                code="binding.low_confidence",
                message="No candidate reached the safe rebinding threshold.",
                candidate_handle=best.handle,
            )
        if len(ranked) > 1 and best_score - second_score < self._margin:
            return BindingDecision(
                character_id=character_id,
                bound=False,
                confidence=best_score,
                code="binding.ambiguous",
                message="Multiple candidates are too similar; player confirmation is required.",
                candidate_handle=best.handle,
            )

        self._registry.confirm_window(
            character_id,
            handle=best.handle,
            process_id=best.process_id,
            window_class=best.window_class,
            rect=best.rect,
            health=WindowHealth.WARNING,
        )
        return BindingDecision(
            character_id=character_id,
            bound=True,
            confidence=best_score,
            code="binding.rebound",
            message="Character was safely rebound to one clear window candidate.",
            candidate_handle=best.handle,
        )


def candidates_from_windows(windows: Iterable[WindowInfo]) -> tuple[WindowCandidate, ...]:
    """Convert basic window observations for binding-engine use."""
    return tuple(WindowCandidate(handle=item.handle, rect=item.rect, title=item.title) for item in windows)
