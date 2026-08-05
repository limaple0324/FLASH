"""Read-only discovery for open game windows outside every saved group."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import WindowBackend, WindowInfo
from services.group_configuration_service import GroupConfigurationService


@dataclass(frozen=True, slots=True)
class UngroupedWindow:
    """One uniquely identified open game window that belongs to no group."""

    shortcut_name: str
    shortcut_path: Path
    fingerprint: str
    status: str


class UngroupedWindowService:
    """Resolve only uniquely matched, player-approved shortcut roots."""

    _STATUS_ONLINE = "online"
    _STATUS_OFFLINE = "offline"
    _STATUS_UNKNOWN = "unknown"

    def __init__(
        self,
        configuration: GroupConfigurationService,
        fingerprint_resolver: ShortcutFingerprintResolver,
        window_backend: WindowBackend,
        screen_states_provider: (
            Callable[[tuple[str, ...], tuple[WindowInfo, ...]], Mapping[str, object]]
            | None
        ) = None,
        *,
        shortcut_roots: Iterable[Path] | None = None,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
    ) -> None:
        if not isinstance(configuration, GroupConfigurationService):
            raise TypeError("configuration must be GroupConfigurationService.")
        if not callable(getattr(fingerprint_resolver, "resolve", None)):
            raise TypeError("fingerprint_resolver must provide resolve(paths).")
        if not callable(getattr(window_backend, "list_windows", None)):
            raise TypeError("window_backend must provide list_windows().")
        if screen_states_provider is not None and not callable(
            screen_states_provider
        ):
            raise TypeError("screen_states_provider must be callable or None.")
        roots = (
            tuple(shortcut_roots)
            if shortcut_roots is not None
            else self.default_shortcut_roots()
        )
        self._shortcut_roots = tuple(
            dict.fromkeys(Path(root) for root in roots)
        )
        self._title_keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._title_keywords:
            raise ValueError("title_keywords must not be empty.")
        self._configuration = configuration
        self._fingerprint_resolver = fingerprint_resolver
        self._window_backend = window_backend
        self._screen_states_provider = screen_states_provider

    @staticmethod
    def default_shortcut_roots() -> tuple[Path, Path]:
        desktop = Path.home() / "Desktop"
        return desktop, desktop / "123"

    @staticmethod
    def _normalized_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
        values: list[Path] = []
        for item in paths:
            path = Path(item).resolve(strict=False)
            try:
                available = path.is_file()
            except OSError:
                available = False
            if path.suffix.casefold() == ".lnk" and available:
                values.append(path)
        return tuple(dict.fromkeys(values))

    def _candidate_shortcuts(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for root in self._shortcut_roots:
            try:
                paths.extend(root.glob("*.lnk"))
            except OSError:
                continue
        return self._normalized_paths(paths)

    def _group_shortcuts(self) -> tuple[Path, ...]:
        return self._normalized_paths(
            entry.shortcut_path
            for group in self._configuration.groups()
            for entry in group.entries
        )

    def _fingerprints_for(
        self,
        paths: tuple[Path, ...],
    ) -> dict[Path, str]:
        try:
            resolved = self._fingerprint_resolver.resolve(paths)
        except Exception:
            return {}
        return {
            path: fingerprint
            for path in paths
            if (
                fingerprint := normalize_launch_fingerprint(
                    resolved.get(path)
                )
            )
            is not None
        }

    @staticmethod
    def _unique_paths_by_fingerprint(
        fingerprints: Mapping[Path, str],
    ) -> dict[str, Path]:
        candidates: dict[str, list[Path]] = {}
        for path, fingerprint in fingerprints.items():
            candidates.setdefault(fingerprint, []).append(path)
        return {
            fingerprint: paths[0]
            for fingerprint, paths in candidates.items()
            if len(paths) == 1
        }

    def snapshot(self) -> tuple[UngroupedWindow, ...]:
        shortcut_paths = self._candidate_shortcuts()
        group_paths = self._group_shortcuts()
        all_paths = tuple(dict.fromkeys((*shortcut_paths, *group_paths)))
        fingerprints = self._fingerprints_for(all_paths)
        shortcut_fingerprints = {
            path: fingerprint
            for path, fingerprint in fingerprints.items()
            if path in shortcut_paths
        }
        grouped_fingerprints = {
            fingerprint
            for path, fingerprint in fingerprints.items()
            if path in group_paths
        }
        paths_by_fingerprint = self._unique_paths_by_fingerprint(
            shortcut_fingerprints
        )
        try:
            windows = tuple(self._window_backend.list_windows())
        except Exception:
            return ()
        by_fingerprint: dict[str, list[WindowInfo]] = {}
        for window in windows:
            if not all(
                keyword in window.title.casefold()
                for keyword in self._title_keywords
            ):
                continue
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is not None:
                by_fingerprint.setdefault(fingerprint, []).append(window)
        resolved = tuple(
            (fingerprint, path, by_fingerprint[fingerprint][0])
            for fingerprint, path in paths_by_fingerprint.items()
            if fingerprint not in grouped_fingerprints
            and len(by_fingerprint.get(fingerprint, ())) == 1
        )
        states: Mapping[str, object] = {}
        if self._screen_states_provider is not None and resolved:
            fingerprints_to_observe = tuple(
                fingerprint for fingerprint, _path, _window in resolved
            )
            windows_to_observe = tuple(
                window for _fingerprint, _path, window in resolved
            )
            try:
                candidate_states = self._screen_states_provider(
                    fingerprints_to_observe,
                    windows_to_observe,
                )
                if isinstance(candidate_states, Mapping):
                    states = candidate_states
            except Exception:
                states = {}
        return tuple(
            UngroupedWindow(
                shortcut_name=path.name,
                shortcut_path=path,
                fingerprint=fingerprint,
                status=self._status_for(states.get(fingerprint)),
            )
            for fingerprint, path, _window in sorted(
                resolved,
                key=lambda item: item[1].name.casefold(),
            )
        )

    def shortcut_for(self, fingerprint: object) -> Path | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None
        shortcut_paths = self._candidate_shortcuts()
        group_paths = self._group_shortcuts()
        all_paths = tuple(dict.fromkeys((*shortcut_paths, *group_paths)))
        fingerprints = self._fingerprints_for(all_paths)
        unique_shortcuts = self._unique_paths_by_fingerprint(
            {
                path: value
                for path, value in fingerprints.items()
                if path in shortcut_paths
            }
        )
        grouped = {
            value
            for path, value in fingerprints.items()
            if path in group_paths
        }
        path = unique_shortcuts.get(normalized)
        if path is None or normalized in grouped:
            return None
        try:
            windows = tuple(self._window_backend.list_windows())
        except Exception:
            return None
        matches = tuple(
            window
            for window in windows
            if all(
                keyword in window.title.casefold()
                for keyword in self._title_keywords
            )
            and normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            == normalized
        )
        return path if len(matches) == 1 else None

    @classmethod
    def _status_for(cls, state: object) -> str:
        value = getattr(state, "value", state)
        if value == "connected":
            return cls._STATUS_ONLINE
        if value == "disconnected":
            return cls._STATUS_OFFLINE
        return cls._STATUS_UNKNOWN
