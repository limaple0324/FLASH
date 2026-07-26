"""Explicit Windows taskbar identity for the player-facing application."""

from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass


APP_USER_MODEL_ID = "Limaple.Fu"

_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850
_VT_EMPTY = 0
_VT_LPWSTR = 31
_GA_ROOT = 2


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_text(cls, value: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = (("fmtid", _GUID), ("pid", wintypes.DWORD))


_propvariant_payload_size = 16 if ctypes.sizeof(ctypes.c_void_p) == 8 else 8


class _PROPVARIANT_VALUE(ctypes.Union):
    _fields_ = (
        ("pointer", ctypes.c_void_p),
        ("padding", ctypes.c_ubyte * _propvariant_payload_size),
    )


class _PROPVARIANT(ctypes.Structure):
    _fields_ = (
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.USHORT),
        ("wReserved2", wintypes.USHORT),
        ("wReserved3", wintypes.USHORT),
        ("value", _PROPVARIANT_VALUE),
    )

    @classmethod
    def text(cls, value: str) -> tuple["_PROPVARIANT", ctypes.Array]:
        buffer = ctypes.create_unicode_buffer(value)
        variant = cls()
        variant.vt = _VT_LPWSTR
        variant.value.pointer = ctypes.cast(buffer, ctypes.c_void_p)
        return variant, buffer

    @classmethod
    def empty(cls) -> "_PROPVARIANT":
        variant = cls()
        variant.vt = _VT_EMPTY
        return variant


_APP_USER_MODEL_FORMAT_ID = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
_PKEY_APP_USER_MODEL_ID = _PROPERTYKEY(
    _GUID.from_text(_APP_USER_MODEL_FORMAT_ID),
    5,
)
_PKEY_APP_USER_MODEL_RELAUNCH_ICON_RESOURCE = _PROPERTYKEY(
    _GUID.from_text(_APP_USER_MODEL_FORMAT_ID),
    3,
)
_IID_IPROPERTY_STORE = _GUID.from_text("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")


def _raise_for_hresult(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"{operation} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")


class _NativeWindowsIdentityBackend:
    """Minimal ctypes bridge to the Windows Shell property system."""

    def __init__(self) -> None:
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._ole32 = ctypes.OleDLL("ole32")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

        self._shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = (
            wintypes.LPCWSTR,
        )
        self._shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
        self._shell32.SHGetPropertyStoreForWindow.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._shell32.SHGetPropertyStoreForWindow.restype = ctypes.HRESULT
        self._ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        self._ole32.CoInitializeEx.restype = ctypes.HRESULT
        self._ole32.CoUninitialize.argtypes = ()
        self._ole32.CoUninitialize.restype = None
        self._user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self._user32.GetAncestor.restype = wintypes.HWND

    def set_process_identity(self, app_id: str) -> None:
        result = self._shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        _raise_for_hresult(result, "SetCurrentProcessExplicitAppUserModelID")

    def root_window_handle(self, window_handle: int) -> int:
        root_handle = self._user32.GetAncestor(window_handle, _GA_ROOT)
        return int(root_handle or window_handle)

    def set_window_identity(
        self,
        window_handle: int,
        app_id: str | None,
        icon_resource: str | None,
    ) -> None:
        initialized = False
        result = self._ole32.CoInitializeEx(None, 2)
        if result in (_S_OK, _S_FALSE):
            initialized = True
        elif result != _RPC_E_CHANGED_MODE:
            _raise_for_hresult(result, "CoInitializeEx")

        property_store = ctypes.c_void_p()
        release = None
        try:
            result = self._shell32.SHGetPropertyStoreForWindow(
                window_handle,
                ctypes.byref(_IID_IPROPERTY_STORE),
                ctypes.byref(property_store),
            )
            _raise_for_hresult(result, "SHGetPropertyStoreForWindow")

            vtable = ctypes.cast(
                property_store,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            set_value = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(_PROPERTYKEY),
                ctypes.POINTER(_PROPVARIANT),
            )(vtable[6])
            commit = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
            )(vtable[7])
            release = ctypes.WINFUNCTYPE(
                wintypes.ULONG,
                ctypes.c_void_p,
            )(vtable[2])

            values = (
                (_PKEY_APP_USER_MODEL_ID, app_id),
                (_PKEY_APP_USER_MODEL_RELAUNCH_ICON_RESOURCE, icon_resource),
            )
            for key, value in values:
                if value is None:
                    variant = _PROPVARIANT.empty()
                    keepalive = None
                else:
                    variant, keepalive = _PROPVARIANT.text(value)
                result = set_value(
                    property_store,
                    ctypes.byref(key),
                    ctypes.byref(variant),
                )
                _raise_for_hresult(result, "IPropertyStore.SetValue")
                _ = keepalive
            _raise_for_hresult(
                commit(property_store),
                "IPropertyStore.Commit",
            )
        finally:
            if property_store.value and release is not None:
                release(property_store)
            if initialized:
                self._ole32.CoUninitialize()


_backend: _NativeWindowsIdentityBackend | None = None


def _windows_backend() -> _NativeWindowsIdentityBackend:
    global _backend
    if _backend is None:
        _backend = _NativeWindowsIdentityBackend()
    return _backend


def configure_process_app_identity(
    app_id: str = APP_USER_MODEL_ID,
) -> bool:
    """Assign the process identity before any window is presented."""
    if sys.platform != "win32":
        return False
    try:
        _windows_backend().set_process_identity(app_id)
    except OSError:
        return False
    return True


@dataclass
class WindowAppIdentity:
    """Applied identity that can release window properties before destruction."""

    window_handle: int
    backend: _NativeWindowsIdentityBackend
    active: bool = True

    def clear(self) -> None:
        if not self.active:
            return
        try:
            self.backend.set_window_identity(self.window_handle, None, None)
        except OSError:
            pass
        finally:
            self.active = False


def configure_tk_window_app_identity(
    window: object,
    icon_resource: str,
    app_id: str = APP_USER_MODEL_ID,
) -> WindowAppIdentity | None:
    """Assign the same explicit identity and icon to a Tk top-level window."""
    if sys.platform != "win32":
        return None
    try:
        raw_handle = int(window.winfo_id())
        backend = _windows_backend()
        root_handle = backend.root_window_handle(raw_handle)
        backend.set_window_identity(root_handle, app_id, icon_resource)
    except (OSError, TypeError, ValueError):
        return None
    return WindowAppIdentity(root_handle, backend)
