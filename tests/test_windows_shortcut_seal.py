import os
from pathlib import Path

import pytest

from adapters.windows_shortcut_seal import (
    ShortcutSealResolutionError,
    Win32ShortcutFileIdentityProvider,
    WindowsShortcutSealResolver,
)
from core.smart_reconnect_authorization import ShortcutFileIdentity


class FakeFingerprintResolver:
    def __init__(self, values):
        self.values = values

    def resolve(self, paths):
        return {Path(path): self.values.get(Path(path)) for path in paths}


class FakeFileIdentityProvider:
    def __init__(self, values):
        self.values = values

    def identity_for(self, path):
        value = self.values.get(Path(path))
        if value is None:
            raise OSError("missing file identity")
        return value


def make_resolver(path, *, volume=10, file_index=20, fingerprint="a" * 64):
    normalized = path.resolve()
    return WindowsShortcutSealResolver(
        FakeFingerprintResolver({normalized: fingerprint}),
        file_identity_provider=FakeFileIdentityProvider(
            {
                normalized: ShortcutFileIdentity(
                    str(normalized),
                    volume,
                    file_index,
                )
            }
        ),
    )


def test_resolve_seals_absolute_path_file_identity_content_and_fingerprint(tmp_path):
    shortcut = tmp_path / "role.lnk"
    shortcut.write_bytes(b"shortcut-one")
    resolver = make_resolver(shortcut)

    seal = resolver.resolve((shortcut,))[shortcut.resolve()]

    assert seal.file_identity.normalized_path == os.path.normcase(
        str(shortcut.resolve())
    )
    assert seal.file_identity.stable_key == (10, 20)
    assert seal.content_sha256 == (
        "d566fb36471a724f44334b85b4ef4fc63c9926b0a5e8e8e58f668f2be3fbeb72"
    )
    assert seal.launch_fingerprint == "a" * 64


@pytest.mark.parametrize("missing", ("path", "file_identity", "content", "fingerprint"))
def test_every_missing_seal_component_fails_the_whole_batch(tmp_path, missing):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    if missing == "path":
        second.unlink()
    paths = tuple(path.resolve(strict=False) for path in (first, second))
    fingerprints = {path: "a" * 64 for path in paths}
    identities = {
        path: ShortcutFileIdentity(str(path), 1, index)
        for index, path in enumerate(paths, start=1)
    }
    if missing == "file_identity":
        identities.pop(paths[1])
    if missing == "fingerprint":
        fingerprints.pop(paths[1])

    def content_reader(path):
        if missing == "content" and path == paths[1]:
            raise OSError("unreadable")
        return path.read_bytes()

    resolver = WindowsShortcutSealResolver(
        FakeFingerprintResolver(fingerprints),
        file_identity_provider=FakeFileIdentityProvider(identities),
        content_reader=content_reader,
    )

    with pytest.raises(ShortcutSealResolutionError):
        resolver.resolve((first, second))


def test_compare_detects_path_file_identity_content_and_fingerprint_changes(tmp_path):
    original = tmp_path / "original.lnk"
    other = tmp_path / "other.lnk"
    original.write_bytes(b"same")
    other.write_bytes(b"same")
    expected = make_resolver(original).resolve((original,))[original.resolve()]

    path_changed = make_resolver(other).resolve((other,))[other.resolve()]
    file_changed = make_resolver(original, file_index=21).resolve((original,))[
        original.resolve()
    ]
    original.write_bytes(b"changed")
    content_changed = make_resolver(original).resolve((original,))[original.resolve()]
    original.write_bytes(b"same")
    fingerprint_changed = make_resolver(
        original,
        fingerprint="b" * 64,
    ).resolve((original,))[original.resolve()]

    assert WindowsShortcutSealResolver.compare(expected, expected) is True
    assert WindowsShortcutSealResolver.compare(expected, path_changed) is False
    assert WindowsShortcutSealResolver.compare(expected, file_changed) is False
    assert WindowsShortcutSealResolver.compare(expected, content_changed) is False
    assert WindowsShortcutSealResolver.compare(expected, fingerprint_changed) is False


def test_revalidate_is_available_without_being_called_during_normal_resolution(tmp_path):
    shortcut = tmp_path / "role.lnk"
    shortcut.write_bytes(b"original")
    resolver = make_resolver(shortcut)
    expected = resolver.resolve((shortcut,))[shortcut.resolve()]

    assert resolver.revalidate(expected) is True
    shortcut.write_bytes(b"rewritten")
    assert resolver.revalidate(expected) is False


def test_windows_file_identity_uses_volume_serial_and_file_index(tmp_path):
    shortcut = tmp_path / "role.lnk"
    shortcut.write_bytes(b"identity-only")

    identity = Win32ShortcutFileIdentityProvider().identity_for(shortcut)

    assert identity.normalized_path == os.path.normcase(str(shortcut.resolve()))
    assert identity.volume_serial_number >= 0
    assert identity.file_index >= 0
