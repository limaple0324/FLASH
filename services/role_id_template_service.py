"""Legacy-compatible role-ID calibration using passive client-region reads."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from adapters.windows_background_capture import CaptureSample
from adapters.windows_sync_calibration import Win32SyncCalibrationBackend


ROLE_ID_REGION = (87, 13, 177, 37)
MIN_FEATURE_PIXELS = 100
MAX_MATCH_SCORE = 0.08


def clean_role_id_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", "", value)
    cleaned = re.sub(r"^[0-9Il]*[|]+", "", cleaned)
    cleaned = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "", cleaned)
    return cleaned[:24]


def signature_from_sample(
    sample: CaptureSample,
) -> tuple[int, int, str, int]:
    if (
        sample.width <= 0
        or sample.height <= 0
        or len(sample.pixels) < sample.width * sample.height * 4
    ):
        return 0, 0, "", 0
    bits: list[str] = []
    for offset in range(0, sample.width * sample.height * 4, 4):
        blue, green, red = sample.pixels[offset : offset + 3]
        bright = max(red, green, blue)
        dark = min(red, green, blue)
        bits.append("1" if bright >= 170 and bright - dark <= 95 else "0")
    signature = "".join(bits)
    return sample.width, sample.height, signature, signature.count("1")


def signature_distance(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right)) + abs(
        len(left) - len(right)
    )


@dataclass(frozen=True, slots=True)
class RoleIdReadResult:
    success: bool
    role_id: str = ""
    message: str = ""
    score: float | None = None


class RoleIdTemplateService:
    def __init__(
        self,
        path: Path,
        *,
        capture_backend: Win32SyncCalibrationBackend | None = None,
    ) -> None:
        self.path = Path(path)
        self._capture_backend = (
            capture_backend or Win32SyncCalibrationBackend()
        )

    def _load(self) -> list[dict[str, object]]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        return [
            dict(item)
            for item in payload
            if isinstance(item, Mapping)
        ] if isinstance(payload, list) else []

    def _save(self, templates: list[dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    templates,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _signature(
        self,
        window_handle: int,
    ) -> tuple[int, int, str, int] | None:
        sample = self._capture_backend.capture_client_region(
            window_handle,
            ROLE_ID_REGION,
        )
        if sample is None or not sample.api_succeeded:
            return None
        return signature_from_sample(sample)

    def calibrate(
        self,
        window_handle: int,
        role_id: object,
        *,
        entry_id: object = "",
    ) -> RoleIdReadResult:
        normalized = clean_role_id_text(role_id)
        bound_entry_id = str(entry_id).strip()
        if not normalized:
            return RoleIdReadResult(False, message="角色ID不可空白。")
        captured = self._signature(window_handle)
        if captured is None:
            return RoleIdReadResult(False, message="無法讀取角色ID區域。")
        width, height, signature, count = captured
        if count < MIN_FEATURE_PIXELS:
            return RoleIdReadResult(
                False,
                message="角色ID區域內沒有足夠文字特徵。",
            )
        templates = [
            item
            for item in self._load()
            if (
                str(item.get("entry_id", "")) != bound_entry_id
                if bound_entry_id
                else str(item.get("role_id", "")) != normalized
            )
        ]
        templates.append(
            {
                "role_id": normalized,
                "width": width,
                "height": height,
                "signature": signature,
                "count": count,
                "region": list(ROLE_ID_REGION),
                "entry_id": bound_entry_id,
            }
        )
        self._save(templates)
        return RoleIdReadResult(
            True,
            role_id=normalized,
            message=f"已保存角色ID範本：{normalized}",
            score=0.0,
        )

    def read(
        self,
        window_handle: int,
        *,
        entry_id: object = "",
    ) -> RoleIdReadResult:
        bound_entry_id = str(entry_id).strip()
        captured = self._signature(window_handle)
        if captured is None:
            return RoleIdReadResult(False, message="無法讀取角色ID區域。")
        width, height, signature, count = captured
        if count < MIN_FEATURE_PIXELS:
            return RoleIdReadResult(
                False,
                message="角色ID區域內沒有足夠文字特徵。",
            )
        best_role = ""
        best_score = 1.0
        for item in self._load():
            if bound_entry_id and (
                str(item.get("entry_id", "")) != bound_entry_id
            ):
                continue
            template_signature = str(item.get("signature", ""))
            if (
                not template_signature
                or item.get("width") != width
                or item.get("height") != height
            ):
                continue
            score = signature_distance(
                signature,
                template_signature,
            ) / max(1, len(signature))
            if score < best_score:
                best_score = score
                best_role = str(item.get("role_id", ""))
        if best_role and best_score <= MAX_MATCH_SCORE:
            return RoleIdReadResult(
                True,
                role_id=best_role,
                message=f"相似度差異 {best_score:.3f}",
                score=best_score,
            )
        if best_role:
            return RoleIdReadResult(
                False,
                message=(
                    f"最接近 {best_role}，但差異過大 "
                    f"{best_score:.3f}"
                ),
                score=best_score,
            )
        return RoleIdReadResult(
            False,
            message="沒有可用的角色ID範本，請先校正角色ID。",
        )
