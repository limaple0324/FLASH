"""Persist one feature card's direct settings as a single user operation."""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, MutableMapping

from config.config_manager import ConfigManager
from services.background_image_service import BackgroundImageService
from services.feature_card_layout_service import (
    FeatureCardLayoutService,
    FeatureCardPreference,
)
from services.feature_hotkey_monitor import normalize_feature_hotkey
from services.group_configuration_service import (
    GroupConfigurationService,
    GroupHotkeyConflictError,
)


@dataclass(frozen=True, slots=True)
class FeatureCardSettingsBatchResult:
    succeeded: bool
    message: str
    preference: FeatureCardPreference | None = None
    background_path: Path | None = None
    hotkey: str = ""


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    existed: bool
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class _GroupFilesSnapshot:
    current: _FileSnapshot
    backup: _FileSnapshot


class FeatureCardSettingsBatchService:
    """Coordinate title, card background and shortcut without partial success."""

    def __init__(
        self,
        *,
        config: ConfigManager,
        feature_card_layout_service: FeatureCardLayoutService,
        background_image_service: BackgroundImageService,
        configured_feature_hotkeys: MutableMapping[str, str],
        feature_hotkeys_config_key: str,
        group_configuration_service: GroupConfigurationService | None = None,
        error_logger: Callable[[str], object] | None = None,
    ) -> None:
        self._config = config
        self._layout = feature_card_layout_service
        self._backgrounds = background_image_service
        self._feature_hotkeys = configured_feature_hotkeys
        self._feature_hotkeys_config_key = feature_hotkeys_config_key
        self._groups = group_configuration_service
        self._error_logger = error_logger

    def save(
        self,
        *,
        card_id: str,
        title: str,
        reset_title: bool,
        pending_background_path: Path | None,
        clear_background: bool,
        hotkey_feature: str | None,
        hotkey: str,
        group_name: str | None,
    ) -> FeatureCardSettingsBatchResult:
        if clear_background and pending_background_path is not None:
            return self._failure("卡片背景變更互相衝突")

        normalized_hotkey = normalize_feature_hotkey(hotkey)
        hotkey_error = self._validate_hotkey(
            hotkey_feature=hotkey_feature,
            hotkey=normalized_hotkey,
            group_name=group_name,
        )
        if hotkey_error:
            return self._failure(hotkey_error)

        group_files_snapshot: _GroupFilesSnapshot | None = None
        if hotkey_feature == "group_launch":
            try:
                group_files_snapshot = self._snapshot_group_files()
            except OSError:
                return self._failure(
                    "整組快捷鍵檔案無法建立安全回復副本"
                )
        previous_config = deepcopy(self._config.data)
        previous_group_hotkey = self._group_hotkey(group_name)
        previous_background = self._backgrounds.current_card_background(card_id)
        background_will_change = (
            pending_background_path is not None or clear_background
        )
        previous_background_bytes = (
            self._read_bytes(previous_background)
            if background_will_change and previous_background is not None
            else None
        )
        if (
            background_will_change
            and previous_background is not None
            and previous_background_bytes is None
        ):
            return self._failure("舊卡片背景無法建立安全回復副本")

        preference: FeatureCardPreference | None = None
        background_path = previous_background
        try:
            with self._config.transaction():
                if reset_title:
                    self._layout.reset_title(card_id)
                    preference = self._layout.preference(card_id, title)
                else:
                    preference = self._layout.set_title(card_id, title)

                if hotkey_feature in self._feature_hotkeys:
                    next_hotkeys = dict(self._feature_hotkeys)
                    next_hotkeys[hotkey_feature] = normalized_hotkey
                    self._config.set(
                        self._feature_hotkeys_config_key,
                        next_hotkeys,
                    )

                if pending_background_path is not None:
                    result = self._backgrounds.commit_prepared_to_card(
                        pending_background_path,
                        card_id,
                    )
                    if not result.succeeded:
                        raise RuntimeError(result.message)
                    background_path = result.managed_path
                elif clear_background:
                    result = self._backgrounds.clear_card(card_id)
                    if not result.succeeded:
                        raise RuntimeError(result.message)
                    background_path = None

            if hotkey_feature == "group_launch":
                self._save_group_hotkey(group_name, normalized_hotkey)
        except Exception as error:
            rollback_errors = self._rollback(
                previous_config=previous_config,
                group_name=group_name,
                previous_group_hotkey=previous_group_hotkey,
                previous_background=previous_background,
                previous_background_bytes=previous_background_bytes,
                changed_background=background_path,
                group_files_snapshot=group_files_snapshot,
            )
            if rollback_errors:
                self._log_rollback_errors(rollback_errors)
                return FeatureCardSettingsBatchResult(
                    False,
                    "卡片設定儲存失敗，而且原設定還原未完成；"
                    "請重新開啟確認。",
                )
            return self._failure(str(error) or "卡片設定無法儲存")

        if hotkey_feature in self._feature_hotkeys:
            self._feature_hotkeys[hotkey_feature] = normalized_hotkey
        return FeatureCardSettingsBatchResult(
            True,
            "卡片名稱、背景與快捷鍵已一起儲存。",
            preference=preference,
            background_path=background_path,
            hotkey=normalized_hotkey,
        )

    def _validate_hotkey(
        self,
        *,
        hotkey_feature: str | None,
        hotkey: str,
        group_name: str | None,
    ) -> str:
        allowed = {
            None,
            *self._feature_hotkeys.keys(),
            "group_launch",
        }
        if hotkey_feature not in allowed:
            return "快捷鍵所屬功能無效"
        if hotkey_feature in self._feature_hotkeys:
            if hotkey and any(
                value == hotkey
                for name, value in self._feature_hotkeys.items()
                if name != hotkey_feature
            ):
                return "快捷鍵已被其他功能使用"
            if (
                hotkey
                and self._groups is not None
                and hotkey in self._groups.launch_hotkeys().values()
            ):
                return "快捷鍵已被整組啟動使用"
            return ""
        if hotkey_feature != "group_launch":
            return ""
        if (
            self._groups is None
            or not isinstance(group_name, str)
            or self._groups.group(group_name) is None
        ):
            return "目前組別設定無效"
        if hotkey and hotkey in self._feature_hotkeys.values():
            return "快捷鍵已被其他功能使用"
        if hotkey and any(
            name != group_name and value == hotkey
            for name, value in self._groups.launch_hotkeys().items()
        ):
            return "快捷鍵已被其他組別使用"
        return ""

    def _group_hotkey(self, group_name: str | None) -> str:
        if self._groups is None or not isinstance(group_name, str):
            return ""
        group = self._groups.group(group_name)
        return group.launch_hotkey if group is not None else ""

    def _save_group_hotkey(
        self,
        group_name: str | None,
        hotkey: str,
    ) -> None:
        if self._groups is None or not isinstance(group_name, str):
            raise RuntimeError("目前組別設定無效")
        try:
            saved = self._groups.set_launch_hotkey(group_name, hotkey)
        except GroupHotkeyConflictError as error:
            raise RuntimeError(error.player_message) from error
        if not saved and self._group_hotkey(group_name) != hotkey:
            raise RuntimeError("整組啟動快捷鍵無法儲存")

    def _rollback(
        self,
        *,
        previous_config: dict[str, object],
        group_name: str | None,
        previous_group_hotkey: str,
        previous_background: Path | None,
        previous_background_bytes: bytes | None,
        changed_background: Path | None,
        group_files_snapshot: _GroupFilesSnapshot | None,
    ) -> list[Exception]:
        errors: list[Exception] = []
        if self._config.data != previous_config:
            try:
                self._config.replace_all(previous_config)
            except Exception as error:
                errors.append(error)
        if (
            self._groups is not None
            and isinstance(group_name, str)
            and self._group_hotkey(group_name) != previous_group_hotkey
        ):
            try:
                self._groups.set_launch_hotkey(
                    group_name,
                    previous_group_hotkey,
                )
            except Exception as error:
                errors.append(error)
        if group_files_snapshot is not None and self._groups is not None:
            for path, snapshot in (
                (self._groups.backup_path, group_files_snapshot.backup),
                (self._groups.path, group_files_snapshot.current),
            ):
                try:
                    self._restore_file(path, snapshot)
                except Exception as error:
                    errors.append(error)
        if (
            previous_background is not None
            and previous_background_bytes is not None
            and not previous_background.is_file()
        ):
            try:
                previous_background.parent.mkdir(parents=True, exist_ok=True)
                temporary = previous_background.with_name(
                    previous_background.name + ".batch-rollback.tmp"
                )
                temporary.write_bytes(previous_background_bytes)
                temporary.replace(previous_background)
            except Exception as error:
                errors.append(error)
        if (
            changed_background is not None
            and changed_background != previous_background
        ):
            self._backgrounds.discard_prepared(changed_background)
        return errors

    def _snapshot_group_files(self) -> _GroupFilesSnapshot:
        if self._groups is None:
            raise OSError("group configuration service is unavailable")
        return _GroupFilesSnapshot(
            current=self._snapshot_file(self._groups.path),
            backup=self._snapshot_file(self._groups.backup_path),
        )

    @staticmethod
    def _snapshot_file(path: Path) -> _FileSnapshot:
        try:
            return _FileSnapshot(True, path.read_bytes())
        except FileNotFoundError:
            return _FileSnapshot(False)

    @staticmethod
    def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
        if not snapshot.existed:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".batch-rollback.tmp")
        try:
            with temporary.open("wb") as file:
                file.write(snapshot.data)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_bytes(path: Path | None) -> bytes | None:
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _failure(self, message: str) -> FeatureCardSettingsBatchResult:
        return FeatureCardSettingsBatchResult(
            False,
            f"{message.rstrip('。')}；全部設定均未變更。",
        )

    def _log_rollback_errors(self, errors: list[Exception]) -> None:
        if self._error_logger is None:
            return
        self._error_logger(
            "卡片設定批次儲存失敗且還原未完整："
            + "；".join(repr(error) for error in errors)
        )
