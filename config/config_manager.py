"""JSON configuration manager for FLASH."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


@dataclass(frozen=True, slots=True)
class ConfigStateSnapshot:
    data: dict[str, Any]
    revision: int


@dataclass(slots=True)
class _TransactionFrame:
    snapshot: dict[str, Any]
    revision: int
    dirty: bool = False


class ConfigManager:
    """Serialize every settings read/write through one re-entrant resource lock."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self._lock = threading.RLock()
        self._thread_local = threading.local()
        self._data: dict[str, Any] = {}
        self._revision = 0
        self.recovered_from_corruption = False
        self.corrupt_backup_path: Path | None = None
        self.load()

    @property
    def data(self) -> dict[str, Any]:
        """Return a deep copy; callers cannot mutate live settings directly."""
        return self.snapshot()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @contextmanager
    def resource_guard(self) -> Iterator["ConfigManager"]:
        """Hold the settings resource across a composed external transaction."""
        with self._lock:
            depth = self._guard_depth()
            self._thread_local.guard_depth = depth + 1
            try:
                yield self
            finally:
                self._thread_local.guard_depth = depth

    def load(self) -> None:
        with self._lock:
            if not self.config_path.exists():
                self._replace_live_data_locked({})
                self._save_now()
                return

            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("Configuration root must be a JSON object.")
                self._replace_live_data_locked(loaded)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._recover_corrupt_config_locked()

    def _recover_corrupt_config_locked(self) -> None:
        """Preserve an unreadable config and rebuild a clean settings file."""
        backup = self.config_path.with_suffix(self.config_path.suffix + ".corrupt")
        counter = 1
        while backup.exists():
            backup = self.config_path.with_suffix(
                self.config_path.suffix + f".corrupt.{counter}"
            )
            counter += 1

        self.config_path.replace(backup)
        self.corrupt_backup_path = backup
        self.recovered_from_corruption = True
        self._replace_live_data_locked({})
        self._save_now()

    def save(self) -> None:
        """Write configuration atomically or defer to the outer transaction."""
        with self._lock:
            frames = self._transaction_frames()
            if frames:
                frames[-1].dirty = True
                return
            self._save_now()

    def _save_now(self) -> None:
        """Write the currently installed candidate while the resource is held."""
        with self._lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
            content = self.serialize_candidate(self._data)
            try:
                with temporary.open("wb") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(self.config_path)
            finally:
                temporary.unlink(missing_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self.transaction():
            replacement = deepcopy(value)
            if self._data.get(key) == replacement and key in self._data:
                return
            self._data[key] = replacement
            self.save()

    def update_values(self, values: Mapping[str, Any]) -> None:
        """Persist only values that actually changed."""
        if not isinstance(values, Mapping):
            raise TypeError("values must be a mapping.")
        with self.transaction():
            changed = False
            for key, value in values.items():
                replacement = deepcopy(value)
                if self._data.get(key) != replacement or key not in self._data:
                    self._data[key] = replacement
                    changed = True
            if changed:
                self.save()

    def ensure_defaults(self, defaults: Mapping[str, Any]) -> None:
        if not isinstance(defaults, Mapping):
            raise TypeError("defaults must be a mapping.")
        with self.transaction():
            changed = False
            for key, value in defaults.items():
                if key not in self._data:
                    self._data[key] = deepcopy(value)
                    changed = True
            if changed:
                self.save()

    @contextmanager
    def transaction(self) -> Iterator["ConfigManager"]:
        """Publish nested same-thread changes once; other threads wait."""
        with self._lock:
            frames = self._transaction_frames()
            frame = _TransactionFrame(
                snapshot=deepcopy(self._data),
                revision=self._revision,
            )
            frames.append(frame)
            try:
                yield self
            except BaseException:
                frames.pop()
                self._data = deepcopy(frame.snapshot)
                self._revision = frame.revision
                raise
            else:
                frames.pop()
                if frames:
                    frames[-1].dirty = frames[-1].dirty or frame.dirty
                    return
                if not frame.dirty:
                    return
                try:
                    self._save_now()
                except BaseException:
                    self._data = deepcopy(frame.snapshot)
                    self._revision = frame.revision
                    raise
                if self._data != frame.snapshot:
                    self._revision = frame.revision + 1

    def replace_all(self, values: Mapping[str, Any]) -> None:
        """Atomically replace all settings through the normal resource lock."""
        if not isinstance(values, Mapping):
            raise TypeError("values must be a mapping.")
        replacement = self._validated_candidate(values)
        with self.transaction():
            if replacement == self._data:
                return
            self._data = replacement
            self.save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def snapshot_with_revision(self) -> ConfigStateSnapshot:
        with self._lock:
            return ConfigStateSnapshot(deepcopy(self._data), self._revision)

    def snapshot_state_locked(self) -> ConfigStateSnapshot:
        self._require_resource_guard()
        return ConfigStateSnapshot(deepcopy(self._data), self._revision)

    def candidate_with_updates_locked(
        self,
        values: Mapping[str, Any],
        *,
        base: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_resource_guard()
        if not isinstance(values, Mapping):
            raise TypeError("values must be a mapping.")
        candidate = deepcopy(dict(base) if base is not None else self._data)
        for key, value in values.items():
            candidate[key] = deepcopy(value)
        return self._validated_candidate(candidate)

    def install_candidate_locked(
        self,
        candidate: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> bool:
        self._require_resource_guard()
        if self._revision != expected_revision:
            raise RuntimeError("configuration changed during candidate publication")
        replacement = self._validated_candidate(candidate)
        if replacement == self._data:
            return False
        self._data = replacement
        self._revision += 1
        return True

    def restore_state_locked(self, snapshot: ConfigStateSnapshot) -> None:
        self._require_resource_guard()
        if not isinstance(snapshot, ConfigStateSnapshot):
            raise TypeError("snapshot must be ConfigStateSnapshot.")
        self._data = self._validated_candidate(snapshot.data)
        self._revision = snapshot.revision

    def ensure_parent_directory_locked(self) -> None:
        self._require_resource_guard()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def serialize_candidate(cls, candidate: Mapping[str, Any]) -> bytes:
        validated = cls._validated_candidate(candidate)
        return json.dumps(
            validated,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    @classmethod
    def validate_serialized_candidate(
        cls,
        content: bytes,
        expected: Mapping[str, Any],
    ) -> bool:
        if not isinstance(content, bytes):
            return False
        try:
            loaded = json.loads(content.decode("utf-8"))
            validated = cls._validated_candidate(loaded)
            expected_candidate = cls._validated_candidate(expected)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        return validated == expected_candidate

    @staticmethod
    def _validated_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, Mapping):
            raise TypeError("configuration candidate must be a mapping")
        cloned = deepcopy(dict(candidate))
        encoded = json.dumps(cloned, ensure_ascii=False)
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise ValueError("Configuration root must be a JSON object.")
        return decoded

    def _replace_live_data_locked(self, values: Mapping[str, Any]) -> None:
        replacement = self._validated_candidate(values)
        if replacement != self._data:
            self._data = replacement
            self._revision += 1

    def _transaction_frames(self) -> list[_TransactionFrame]:
        frames = getattr(self._thread_local, "transaction_frames", None)
        if frames is None:
            frames = []
            self._thread_local.transaction_frames = frames
        return frames

    def _guard_depth(self) -> int:
        return int(getattr(self._thread_local, "guard_depth", 0))

    def _require_resource_guard(self) -> None:
        if self._guard_depth() <= 0:
            raise RuntimeError("configuration resource guard is not held")
