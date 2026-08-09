"""Persist one feature card's direct settings as a single user operation."""

from __future__ import annotations

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
from services.identity_data_transaction_coordinator import (
    IdentityDataTransaction,
    IdentityTransactionClosedError,
    IdentityDataTransactionCoordinator,
)


@dataclass(frozen=True, slots=True)
class FeatureCardSettingsBatchResult:
    succeeded: bool
    message: str
    preference: FeatureCardPreference | None = None
    background_path: Path | None = None
    hotkey: str = ""


class _FeatureHotkeyRollbackError(RuntimeError):
    def __init__(
        self,
        original_error: BaseException,
        rollback_errors: tuple[Exception, ...],
    ) -> None:
        super().__init__("快捷鍵發布失敗，而且回復未完成")
        self.original_error = original_error
        self.rollback_errors = rollback_errors


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
        coordinator: IdentityDataTransactionCoordinator,
        group_configuration_service: GroupConfigurationService | None = None,
        error_logger: Callable[[str], object] | None = None,
    ) -> None:
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator.")
        if (
            group_configuration_service is not None
            and group_configuration_service.coordinator is not coordinator
        ):
            raise ValueError(
                "group_configuration_service must use the injected coordinator."
            )
        self._config = config
        self._layout = feature_card_layout_service
        self._backgrounds = background_image_service
        self._feature_hotkeys = configured_feature_hotkeys
        self._feature_hotkeys_config_key = feature_hotkeys_config_key
        self._coordinator = coordinator
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
        with self._config.resource_guard():
            return self._save_locked(
                card_id=card_id,
                title=title,
                reset_title=reset_title,
                pending_background_path=pending_background_path,
                clear_background=clear_background,
                hotkey_feature=hotkey_feature,
                hotkey=hotkey,
                group_name=group_name,
            )

    def change_feature_hotkey(
        self,
        feature: str,
        hotkey: str,
    ) -> FeatureCardSettingsBatchResult:
        """Linearize one feature shortcut against every group shortcut."""
        with self._config.resource_guard():
            normalized = normalize_feature_hotkey(hotkey)
            try:
                self._publish_feature_hotkey_locked(
                    feature,
                    normalized,
                )
            except _FeatureHotkeyRollbackError as error:
                self._log_rollback_errors(list(error.rollback_errors))
                return FeatureCardSettingsBatchResult(
                    False,
                    "快捷鍵儲存失敗，而且設定與執行狀態回復未完成；"
                    "請重新開啟確認。",
                )
            except Exception as error:
                return self._failure(str(error) or "快捷鍵無法儲存")
            return FeatureCardSettingsBatchResult(
                True,
                "快捷鍵已儲存。",
                hotkey=normalized,
            )

    def change_group_launch_hotkey(
        self,
        group_name: str,
        hotkey: str,
    ) -> FeatureCardSettingsBatchResult:
        """Linearize one group shortcut against every feature shortcut."""
        with self._config.resource_guard():
            normalized = normalize_feature_hotkey(hotkey)
            error = self._validate_hotkey(
                hotkey_feature="group_launch",
                hotkey=normalized,
                group_name=group_name,
            )
            if error:
                return self._failure(error)
            try:
                self._save_group_hotkey(group_name, normalized)
            except Exception as error:
                return self._failure(str(error) or "整組啟動快捷鍵無法儲存")
            return FeatureCardSettingsBatchResult(
                True,
                "整組啟動快捷鍵已儲存。",
                hotkey=normalized,
            )

    def _save_locked(
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
            check_group_conflicts=False,
        )
        if hotkey_error:
            return self._failure(hotkey_error)
        if hotkey_feature in self._feature_hotkeys:
            try:
                self._check_feature_hotkey_locked(
                    hotkey_feature,
                    normalized_hotkey,
                )
            except Exception as error:
                return self._failure(str(error) or "快捷鍵無法儲存")

        previous_config = self._config.snapshot()
        previous_feature_hotkeys = dict(self._feature_hotkeys)
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
            elif hotkey_feature in self._feature_hotkeys:
                self._publish_feature_hotkey_locked(
                    hotkey_feature,
                    normalized_hotkey,
                )
        except Exception as error:
            rollback_errors = self._rollback(
                previous_config=previous_config,
                previous_feature_hotkeys=previous_feature_hotkeys,
                previous_background=previous_background,
                previous_background_bytes=previous_background_bytes,
                changed_background=background_path,
            )
            if isinstance(error, _FeatureHotkeyRollbackError):
                rollback_errors[:0] = error.rollback_errors
            if rollback_errors:
                self._log_rollback_errors(rollback_errors)
                return FeatureCardSettingsBatchResult(
                    False,
                    "卡片設定儲存失敗，而且原設定還原未完成；"
                    "請重新開啟確認。",
                )
            return self._failure(str(error) or "卡片設定無法儲存")

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
        check_group_conflicts: bool = True,
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
                check_group_conflicts
                and hotkey
                and self._groups is not None
            ):
                try:
                    group_hotkeys = self._groups.launch_hotkeys()
                except IdentityTransactionClosedError:
                    return "身分資料交易已停止，無法檢查整組快捷鍵"
                if hotkey in group_hotkeys.values():
                    return "快捷鍵已被整組啟動使用"
            return ""
        if hotkey_feature != "group_launch":
            return ""
        if (
            self._groups is None
            or not isinstance(group_name, str)
            or not group_name.strip()
        ):
            return "目前組別設定無效"
        if hotkey and hotkey in self._feature_hotkeys.values():
            return "快捷鍵已被其他功能使用"
        return ""

    def _publish_feature_hotkey_locked(
        self,
        feature: str,
        hotkey: str,
    ) -> None:
        def publish() -> None:
            previous_config = self._config.snapshot()
            previous_feature_hotkeys = dict(self._feature_hotkeys)
            try:
                self._require_feature_hotkey_available(feature, hotkey)
                next_hotkeys = dict(self._feature_hotkeys)
                next_hotkeys[feature] = hotkey
                self._config.set(
                    self._feature_hotkeys_config_key,
                    next_hotkeys,
                )
                self._feature_hotkeys[feature] = hotkey
            except BaseException as error:
                rollback_errors = self._restore_feature_hotkey_publication(
                    previous_config,
                    previous_feature_hotkeys,
                )
                if rollback_errors:
                    raise _FeatureHotkeyRollbackError(
                        error,
                        tuple(rollback_errors),
                    ) from error
                raise

        self._coordinator.read_consistent(publish)

    def _check_feature_hotkey_locked(
        self,
        feature: str,
        hotkey: str,
    ) -> None:
        self._coordinator.read_consistent(
            lambda: self._require_feature_hotkey_available(feature, hotkey)
        )

    def _require_feature_hotkey_available(
        self,
        feature: str,
        hotkey: str,
    ) -> None:
        error = self._validate_hotkey(
            hotkey_feature=feature,
            hotkey=hotkey,
            group_name=None,
        )
        if error:
            raise RuntimeError(error)

    def _save_group_hotkey(
        self,
        group_name: str | None,
        hotkey: str,
    ) -> None:
        if self._groups is None or not isinstance(group_name, str):
            raise RuntimeError("目前組別設定無效")
        cleaned_group_name = group_name.strip()
        if not cleaned_group_name:
            raise RuntimeError("目前組別設定無效")
        try:
            self._coordinator.execute(
                lambda transaction: self._stage_group_hotkey(
                    transaction,
                    cleaned_group_name,
                    hotkey,
                )
            )
        except GroupHotkeyConflictError as error:
            raise RuntimeError(error.player_message) from error

    def _stage_group_hotkey(
        self,
        transaction: IdentityDataTransaction,
        group_name: str,
        hotkey: str,
    ) -> None:
        if self._groups is None:
            raise RuntimeError("目前組別設定無效")

        def mutation(candidate) -> None:
            group = candidate.group(group_name)
            if group is None:
                raise RuntimeError("目前組別設定無效")
            saved = candidate.set_launch_hotkey(group_name, hotkey)
            current = candidate.group(group_name)
            if current is None or (not saved and current.launch_hotkey != hotkey):
                raise RuntimeError("整組啟動快捷鍵無法儲存")

        self._groups.stage_candidate(transaction, mutation)

    def _rollback(
        self,
        *,
        previous_config: dict[str, object],
        previous_feature_hotkeys: dict[str, str],
        previous_background: Path | None,
        previous_background_bytes: bytes | None,
        changed_background: Path | None,
    ) -> list[Exception]:
        errors: list[Exception] = []
        if self._config.snapshot() != previous_config:
            try:
                self._config.replace_all(previous_config)
            except Exception as error:
                errors.append(error)
        if dict(self._feature_hotkeys) != previous_feature_hotkeys:
            try:
                self._restore_feature_hotkeys(previous_feature_hotkeys)
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

    def _restore_feature_hotkey_publication(
        self,
        previous_config: dict[str, object],
        previous_feature_hotkeys: dict[str, str],
    ) -> list[Exception]:
        errors: list[Exception] = []
        try:
            self._restore_feature_hotkeys(previous_feature_hotkeys)
        except Exception as error:
            errors.append(error)
        if self._config.snapshot() != previous_config:
            try:
                self._config.replace_all(previous_config)
            except Exception as error:
                errors.append(error)
        return errors

    def _restore_feature_hotkeys(
        self,
        snapshot: dict[str, str],
    ) -> None:
        self._feature_hotkeys.clear()
        self._feature_hotkeys.update(snapshot)
        if dict(self._feature_hotkeys) != snapshot:
            raise RuntimeError("快捷鍵執行狀態回復後不一致")

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
