"""已確認的每角色遊戲延伸資料快照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _nonnegative_int(
    value: object,
    field_name: str,
    *,
    allow_none: bool = False,
) -> int | None:
    if value is None and allow_none:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


@dataclass(frozen=True, slots=True)
class PetTalentPageSnapshot:
    """一頁已可靠辨識的寵物天賦畫面文字。"""

    page_number: int
    observed_text: str
    updated_at: str
    content_signature: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or not 1 <= self.page_number <= 4
        ):
            raise ValueError("page_number must be between 1 and 4.")
        object.__setattr__(
            self,
            "observed_text",
            _required_text(self.observed_text, "observed_text"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, "updated_at"),
        )
        object.__setattr__(
            self,
            "content_signature",
            _optional_text(self.content_signature, "content_signature"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "observed_text": self.observed_text,
            "updated_at": self.updated_at,
            "content_signature": self.content_signature,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PetTalentPageSnapshot":
        return cls(
            page_number=payload.get("page_number"),  # type: ignore[arg-type]
            observed_text=payload.get("observed_text"),  # type: ignore[arg-type]
            updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
            content_signature=payload.get("content_signature"),
        )


@dataclass(frozen=True, slots=True)
class PetTalentSnapshot:
    """四頁寵物天賦資料；只保存實際可靠讀到的頁面。"""

    pages: tuple[PetTalentPageSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("pages must contain at least one page.")
        if any(not isinstance(item, PetTalentPageSnapshot) for item in self.pages):
            raise TypeError("pages must contain only PetTalentPageSnapshot values.")
        numbers = [item.page_number for item in self.pages]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Pet talent page numbers must be unique.")

    @property
    def complete(self) -> bool:
        return {item.page_number for item in self.pages} == {1, 2, 3, 4}

    def to_dict(self) -> dict[str, object]:
        return {"pages": [item.to_dict() for item in self.pages]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PetTalentSnapshot":
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, list) or any(
            not isinstance(item, Mapping) for item in raw_pages
        ):
            raise ValueError("pages must be a list of objects.")
        return cls(
            pages=tuple(PetTalentPageSnapshot.from_dict(item) for item in raw_pages)
        )


@dataclass(frozen=True, slots=True)
class ObsidianSnapshot:
    """黑曜石只保存頁面、階段與節點狀態，不保存格位名稱或操作。"""

    opened_page: int
    unlit_nodes: int
    updated_at: str
    stage: str | None = None
    opened_nodes: int | None = None
    page_shape_signature: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.opened_page, bool)
            or not isinstance(self.opened_page, int)
            or not 1 <= self.opened_page <= 10
        ):
            raise ValueError("opened_page must be between 1 and 10.")
        _nonnegative_int(self.unlit_nodes, "unlit_nodes")
        _nonnegative_int(self.opened_nodes, "opened_nodes", allow_none=True)
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, "updated_at"),
        )
        object.__setattr__(self, "stage", _optional_text(self.stage, "stage"))
        object.__setattr__(
            self,
            "page_shape_signature",
            _optional_text(self.page_shape_signature, "page_shape_signature"),
        )

    @property
    def verified(self) -> bool:
        """只有同時有階段、已開格數與頁形證據才可作為新讀值。"""
        return bool(
            self.stage
            and self.opened_nodes is not None
            and self.page_shape_signature
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "opened_page": self.opened_page,
            "unlit_nodes": self.unlit_nodes,
            "updated_at": self.updated_at,
            "stage": self.stage,
            "opened_nodes": self.opened_nodes,
            "page_shape_signature": self.page_shape_signature,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ObsidianSnapshot":
        return cls(
            opened_page=payload.get("opened_page"),  # type: ignore[arg-type]
            unlit_nodes=payload.get("unlit_nodes"),  # type: ignore[arg-type]
            updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
            stage=payload.get("stage"),
            opened_nodes=payload.get("opened_nodes"),
            page_shape_signature=payload.get("page_shape_signature"),
        )


@dataclass(frozen=True, slots=True)
class ObsidianPagesSnapshot:
    """同一角色已可靠讀到的黑曜石每頁最新快照。"""

    pages: tuple[ObsidianSnapshot, ...]

    def __post_init__(self) -> None:
        try:
            pages = tuple(self.pages)
        except TypeError as error:
            raise TypeError("pages must be an iterable of ObsidianSnapshot values.") from error
        if not pages:
            raise ValueError("pages must contain at least one page.")
        if any(not isinstance(item, ObsidianSnapshot) for item in pages):
            raise TypeError("pages must contain only ObsidianSnapshot values.")
        numbers = [item.opened_page for item in pages]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Obsidian page numbers must be unique.")
        object.__setattr__(
            self,
            "pages",
            tuple(sorted(pages, key=lambda item: item.opened_page)),
        )

    @property
    def read_page_count(self) -> int:
        return len(self.pages)

    @property
    def highest_read_page(self) -> int:
        return self.pages[-1].opened_page

    @property
    def total_unlit_nodes(self) -> int:
        return sum(item.unlit_nodes for item in self.pages)

    def to_dict(self) -> dict[str, object]:
        return {"pages": [item.to_dict() for item in self.pages]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ObsidianPagesSnapshot":
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, list) or any(
            not isinstance(item, Mapping) for item in raw_pages
        ):
            raise ValueError("pages must be a list of objects.")
        return cls(
            pages=tuple(ObsidianSnapshot.from_dict(item) for item in raw_pages)
        )


@dataclass(frozen=True, slots=True)
class LifeSoul:
    name: str
    level: int
    effect: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        _nonnegative_int(self.level, "level")
        object.__setattr__(self, "effect", _required_text(self.effect, "effect"))

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "level": self.level, "effect": self.effect}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LifeSoul":
        return cls(
            name=payload.get("name"),  # type: ignore[arg-type]
            level=payload.get("level"),  # type: ignore[arg-type]
            effect=payload.get("effect"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PetLifeSoulSnapshot:
    pet_name: str
    souls: tuple[LifeSoul, ...]
    updated_at: str
    page_number: int = 1
    pet_identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pet_name", _required_text(self.pet_name, "pet_name"))
        if any(not isinstance(item, LifeSoul) for item in self.souls):
            raise TypeError("souls must contain only LifeSoul values.")
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, "updated_at"),
        )
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or not 1 <= self.page_number <= 2
        ):
            raise ValueError("page_number must be 1 or 2.")
        object.__setattr__(
            self,
            "pet_identity",
            _optional_text(self.pet_identity, "pet_identity"),
        )

    @property
    def identity_key(self) -> str:
        return self.pet_identity or self.pet_name

    def to_dict(self) -> dict[str, object]:
        return {
            "pet_name": self.pet_name,
            "souls": [item.to_dict() for item in self.souls],
            "updated_at": self.updated_at,
            "page_number": self.page_number,
            "pet_identity": self.pet_identity,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "PetLifeSoulSnapshot":
        raw_souls = payload.get("souls")
        if not isinstance(raw_souls, list) or any(
            not isinstance(item, Mapping) for item in raw_souls
        ):
            raise ValueError("souls must be a list of objects.")
        return cls(
            pet_name=payload.get("pet_name"),  # type: ignore[arg-type]
            souls=tuple(LifeSoul.from_dict(item) for item in raw_souls),
            updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
            page_number=payload.get("page_number", 1),  # type: ignore[arg-type]
            pet_identity=payload.get("pet_identity"),
        )


def _text_lines(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings.")
    lines: list[str] = []
    for line in value:
        if not isinstance(line, str):
            raise ValueError(f"{field_name} must contain only strings.")
        cleaned = line.strip()
        if cleaned:
            lines.append(cleaned)
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    """魂器只保存已開啟頁面的文字資料，不辨識符文圖示。"""

    page_name: str
    level: int | None
    rune_text: tuple[str, ...]
    summary_lines: tuple[str, ...]
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_name", _required_text(self.page_name, "page_name"))
        _nonnegative_int(self.level, "level", allow_none=True)
        object.__setattr__(self, "rune_text", _text_lines(self.rune_text, "rune_text"))
        object.__setattr__(self, "summary_lines", _text_lines(self.summary_lines, "summary_lines"))
        object.__setattr__(self, "updated_at", _required_text(self.updated_at, "updated_at"))

    def to_dict(self) -> dict[str, object]:
        return {
            "page_name": self.page_name,
            "level": self.level,
            "rune_text": list(self.rune_text),
            "summary_lines": list(self.summary_lines),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ArtifactSnapshot":
        return cls(
            page_name=payload.get("page_name"),  # type: ignore[arg-type]
            level=payload.get("level"),  # type: ignore[arg-type]
            rune_text=payload.get("rune_text", []),  # type: ignore[arg-type]
            summary_lines=payload.get("summary_lines", []),  # type: ignore[arg-type]
            updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CharacterGameData:
    """每個角色四項遊戲資料的最新唯讀快照。"""

    character_id: str
    cultivated_pet_count: int = 0
    obsidian: ObsidianPagesSnapshot | ObsidianSnapshot | None = None
    life_souls: tuple[PetLifeSoulSnapshot, ...] = ()
    pet_talent: PetTalentSnapshot | None = None
    artifact: ArtifactSnapshot | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "character_id",
            _required_text(self.character_id, "character_id"),
        )
        _nonnegative_int(self.cultivated_pet_count, "cultivated_pet_count")
        if isinstance(self.obsidian, ObsidianSnapshot):
            object.__setattr__(
                self,
                "obsidian",
                ObsidianPagesSnapshot((self.obsidian,)),
            )
        elif self.obsidian is not None and not isinstance(
            self.obsidian,
            ObsidianPagesSnapshot,
        ):
            raise TypeError(
                "obsidian must be ObsidianPagesSnapshot, ObsidianSnapshot, or None."
            )
        if self.pet_talent is not None and not isinstance(self.pet_talent, PetTalentSnapshot):
            raise TypeError("pet_talent must be PetTalentSnapshot or None.")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactSnapshot):
            raise TypeError("artifact must be ArtifactSnapshot or None.")
        if any(not isinstance(item, PetLifeSoulSnapshot) for item in self.life_souls):
            raise TypeError("life_souls must contain only PetLifeSoulSnapshot values.")
        keys = [(item.identity_key, item.page_number) for item in self.life_souls]
        if len(keys) != len(set(keys)):
            raise ValueError("Life soul pet pages must be unique.")
        if len({item.identity_key for item in self.life_souls}) > self.cultivated_pet_count:
            raise ValueError("read pet count cannot exceed cultivated pet count.")

    @property
    def read_pet_count(self) -> int:
        return len({item.identity_key for item in self.life_souls})

    def to_dict(self) -> dict[str, object]:
        return {
            "character_id": self.character_id,
            "cultivated_pet_count": self.cultivated_pet_count,
            "obsidian": self.obsidian.to_dict() if self.obsidian is not None else None,
            "life_souls": [item.to_dict() for item in self.life_souls],
            "pet_talent": self.pet_talent.to_dict() if self.pet_talent is not None else None,
            "artifact": self.artifact.to_dict() if self.artifact is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CharacterGameData":
        raw_obsidian = payload.get("obsidian")
        if raw_obsidian is not None and not isinstance(raw_obsidian, Mapping):
            raise ValueError("obsidian must be an object or null.")
        raw_life_souls = payload.get("life_souls", [])
        if not isinstance(raw_life_souls, list) or any(
            not isinstance(item, Mapping) for item in raw_life_souls
        ):
            raise ValueError("life_souls must be a list of objects.")
        raw_pet_talent = payload.get("pet_talent")
        if raw_pet_talent is not None and not isinstance(raw_pet_talent, Mapping):
            raise ValueError("pet_talent must be an object or null.")
        raw_artifact = payload.get("artifact")
        if raw_artifact is not None and not isinstance(raw_artifact, Mapping):
            raise ValueError("artifact must be an object or null.")
        return cls(
            character_id=payload.get("character_id"),  # type: ignore[arg-type]
            cultivated_pet_count=payload.get("cultivated_pet_count", 0),  # type: ignore[arg-type]
            obsidian=(
                ObsidianPagesSnapshot.from_dict(raw_obsidian)
                if raw_obsidian is not None and "pages" in raw_obsidian
                else (
                    ObsidianPagesSnapshot(
                        (ObsidianSnapshot.from_dict(raw_obsidian),)
                    )
                    if raw_obsidian is not None
                    else None
                )
            ),
            life_souls=tuple(PetLifeSoulSnapshot.from_dict(item) for item in raw_life_souls),
            pet_talent=(
                PetTalentSnapshot.from_dict(raw_pet_talent)
                if raw_pet_talent is not None
                else None
            ),
            artifact=(
                ArtifactSnapshot.from_dict(raw_artifact)
                if raw_artifact is not None
                else None
            ),
        )
