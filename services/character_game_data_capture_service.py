"""安全背景擷取與四項遊戲資料的部分更新接點。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from adapters.windows_background_capture import (
    CaptureSample,
    WindowCaptureProvider,
)
from domain.character_game_data import (
    ArtifactSnapshot,
    ObsidianSnapshot,
    PetLifeSoulSnapshot,
    PetTalentPageSnapshot,
    PetTalentSnapshot,
)
from services.character_game_data_update_service import (
    CharacterGameDataUpdateResult,
    CharacterGameDataUpdateService,
)


class GameDataPageKind(str, Enum):
    PET_TALENT = "pet_talent"
    OBSIDIAN = "obsidian"
    LIFE_SOUL = "life_soul"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class VerifiedGameDataPage:
    """由獨立頁面辨識器確認後才可交給保存層的資料。"""

    character_id: str
    page_kind: GameDataPageKind
    content_signature: str
    data: (
        PetTalentPageSnapshot
        | PetTalentSnapshot
        | ObsidianSnapshot
        | PetLifeSoulSnapshot
        | ArtifactSnapshot
    )

    def __post_init__(self) -> None:
        if not isinstance(self.character_id, str) or not self.character_id.strip():
            raise ValueError("character_id must be a non-empty string.")
        if not isinstance(self.page_kind, GameDataPageKind):
            raise TypeError("page_kind must be GameDataPageKind.")
        if not isinstance(self.content_signature, str) or not self.content_signature.strip():
            raise ValueError("content_signature must be a non-empty string.")
        expected_types = {
            GameDataPageKind.PET_TALENT: (PetTalentPageSnapshot, PetTalentSnapshot),
            GameDataPageKind.OBSIDIAN: (ObsidianSnapshot,),
            GameDataPageKind.LIFE_SOUL: (PetLifeSoulSnapshot,),
            GameDataPageKind.ARTIFACT: (ArtifactSnapshot,),
        }
        if not isinstance(self.data, expected_types[self.page_kind]):
            raise TypeError("data does not match page_kind.")


class GameDataPageRecognizer(Protocol):
    def read(self, sample: CaptureSample) -> VerifiedGameDataPage | None:
        """只從目前擷取畫面回傳可靠頁面；無法確認時回傳空值。"""


class GameDataReadStatus(str, Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    TARGET_NOT_ELIGIBLE = "target_not_eligible"
    CAPTURE_UNAVAILABLE = "capture_unavailable"
    PAGE_UNRECOGNIZED = "page_unrecognized"
    IDENTITY_MISMATCH = "identity_mismatch"
    UPDATE_REJECTED = "update_rejected"


@dataclass(frozen=True, slots=True)
class GameDataReadResult:
    status: GameDataReadStatus
    page: VerifiedGameDataPage | None = None
    update: CharacterGameDataUpdateResult | None = None
    error: str | None = None


class CharacterGameDataCaptureService:
    """使用不切前景、不送輸入的擷取器，將可靠頁面交給部分更新服務。"""

    def __init__(
        self,
        capture_provider: WindowCaptureProvider,
        page_recognizer: GameDataPageRecognizer,
        update_service: CharacterGameDataUpdateService,
        target_guard: Callable[[int, str], bool],
    ) -> None:
        if not hasattr(capture_provider, "capture"):
            raise TypeError("capture_provider must provide capture().")
        if not callable(getattr(page_recognizer, "read", None)):
            raise TypeError("page_recognizer must provide read().")
        if not isinstance(update_service, CharacterGameDataUpdateService):
            raise TypeError("update_service must be CharacterGameDataUpdateService.")
        if not callable(target_guard):
            raise TypeError("target_guard must be callable.")
        self._capture_provider = capture_provider
        self._page_recognizer = page_recognizer
        self._update_service = update_service
        self._target_guard = target_guard
        self._signatures: dict[tuple[int, str, GameDataPageKind], str] = {}

    def read(self, window_handle: int, character_id: str) -> GameDataReadResult:
        normalized_character_id = character_id.strip() if isinstance(character_id, str) else ""
        if not normalized_character_id:
            return GameDataReadResult(
                GameDataReadStatus.TARGET_NOT_ELIGIBLE,
                error="角色識別資料無效。",
            )
        try:
            eligible = self._target_guard(int(window_handle), normalized_character_id)
        except Exception as error:
            return GameDataReadResult(
                GameDataReadStatus.TARGET_NOT_ELIGIBLE,
                error=str(error) or "目標視窗資格檢查失敗。",
            )
        if not eligible:
            return GameDataReadResult(
                GameDataReadStatus.TARGET_NOT_ELIGIBLE,
                error="視窗未通過已註冊遊戲視窗資格檢查。",
            )
        try:
            sample = self._capture_provider.capture(window_handle)
        except Exception as error:
            return GameDataReadResult(
                GameDataReadStatus.CAPTURE_UNAVAILABLE,
                error=str(error) or "視窗擷取失敗。",
            )
        if sample is None or not sample.api_succeeded:
            return GameDataReadResult(
                GameDataReadStatus.CAPTURE_UNAVAILABLE,
                error="視窗擷取未成功。",
            )
        try:
            page = self._page_recognizer.read(sample)
        except Exception as error:
            return GameDataReadResult(
                GameDataReadStatus.PAGE_UNRECOGNIZED,
                error=str(error) or "頁面辨識失敗。",
            )
        if page is None:
            return GameDataReadResult(
                GameDataReadStatus.PAGE_UNRECOGNIZED,
                error="目前畫面不是已確認的資料頁。",
            )
        if page.character_id.strip() != normalized_character_id:
            return GameDataReadResult(
                GameDataReadStatus.IDENTITY_MISMATCH,
                page=page,
                error="頁面角色識別與目標不一致。",
            )
        signature_key = (int(window_handle), normalized_character_id, page.page_kind)
        if self._signatures.get(signature_key) == page.content_signature:
            return GameDataReadResult(GameDataReadStatus.UNCHANGED, page=page)
        try:
            update = self._apply(page)
        except (TypeError, ValueError) as error:
            return GameDataReadResult(
                GameDataReadStatus.UPDATE_REJECTED,
                page=page,
                error=str(error) or "資料保存被拒絕。",
            )
        self._signatures[signature_key] = page.content_signature
        if not update.changed:
            return GameDataReadResult(
                GameDataReadStatus.UNCHANGED,
                page=page,
                update=update,
            )
        return GameDataReadResult(
            GameDataReadStatus.UPDATED,
            page=page,
            update=update,
        )

    def clear_window(self, window_handle: int) -> None:
        prefix = int(window_handle)
        self._signatures = {
            key: value
            for key, value in self._signatures.items()
            if key[0] != prefix
        }

    def _apply(self, page: VerifiedGameDataPage) -> CharacterGameDataUpdateResult:
        if page.page_kind is GameDataPageKind.PET_TALENT:
            return self._update_service.update(page.character_id, pet_talent=page.data)
        if page.page_kind is GameDataPageKind.OBSIDIAN:
            return self._update_service.update(page.character_id, obsidian=page.data)
        if page.page_kind is GameDataPageKind.LIFE_SOUL:
            return self._update_service.update(page.character_id, life_souls=page.data)
        return self._update_service.update(page.character_id, artifact=page.data)
