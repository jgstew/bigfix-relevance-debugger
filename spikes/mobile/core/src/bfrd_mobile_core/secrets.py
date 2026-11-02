"""Credential storage: the Keychain on Apple platforms, memory elsewhere.

The Keychain is reached with ``ctypes`` against Security.framework, so the
same code runs on macOS and iOS under any UI toolkit (no pyobjus, rubicon or
Swift needed). Android's Keystore would be another :class:`SecretStore`.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Protocol

log = logging.getLogger(__name__)

SERVICE = "com.github.jgstew.bigfix-relevance-debugger"


class SecretStore(Protocol):
    name: str

    def get(self, account: str) -> str | None: ...
    def set(self, account: str, secret: str) -> None: ...
    def delete(self, account: str) -> None: ...


class MemorySecretStore:
    name = "memory (not saved)"

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self._items.get(account)

    def set(self, account: str, secret: str) -> None:
        self._items[account] = secret

    def delete(self, account: str) -> None:
        self._items.pop(account, None)


class KeychainError(RuntimeError):
    def __init__(self, op: str, status: int) -> None:
        super().__init__(f"Keychain {op} failed: OSStatus {status}")
        self.status = status


_ERR_ITEM_NOT_FOUND = -25300
_ERR_DUPLICATE_ITEM = -25299
_UTF8 = 0x08000100


class KeychainSecretStore:
    """Generic-password items under one service, keyed by account."""

    name = "Keychain"

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        vp = ctypes.c_void_p
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDataCreate.restype = vp
        cf.CFDataCreate.argtypes = [vp, ctypes.c_char_p, ctypes.c_long]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [
            vp,
            ctypes.POINTER(vp),
            ctypes.POINTER(vp),
            ctypes.c_long,
            vp,
            vp,
        ]
        cf.CFRelease.argtypes = [vp]
        for fn in ("SecItemAdd", "SecItemCopyMatching"):
            getattr(sec, fn).restype = ctypes.c_int32
            getattr(sec, fn).argtypes = [vp, ctypes.POINTER(vp)]
        sec.SecItemDelete.restype = ctypes.c_int32
        sec.SecItemDelete.argtypes = [vp]
        sec.SecItemUpdate.restype = ctypes.c_int32
        sec.SecItemUpdate.argtypes = [vp, vp]
        self._cf, self._sec = cf, sec

        def const(lib: ctypes.CDLL, name: str) -> int:
            return vp.in_dll(lib, name).value  # type: ignore[return-value]

        self._k = {
            n: const(sec, n)
            for n in (
                "kSecClass",
                "kSecClassGenericPassword",
                "kSecAttrService",
                "kSecAttrAccount",
                "kSecValueData",
                "kSecReturnData",
                "kSecMatchLimit",
                "kSecMatchLimitOne",
                "kSecAttrAccessible",
                "kSecAttrAccessibleWhenUnlockedThisDeviceOnly",
            )
        }
        self._true = const(cf, "kCFBooleanTrue")
        self._key_cb = ctypes.addressof(vp.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self._val_cb = ctypes.addressof(vp.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))

    # -- CoreFoundation helpers -------------------------------------------

    def _str(self, s: str) -> int:
        return self._cf.CFStringCreateWithCString(None, s.encode(), _UTF8)

    def _dict(self, pairs: list[tuple[int, int]]) -> int:
        n = len(pairs)
        keys = (ctypes.c_void_p * n)(*[k for k, _ in pairs])
        vals = (ctypes.c_void_p * n)(*[v for _, v in pairs])
        return self._cf.CFDictionaryCreate(None, keys, vals, n, self._key_cb, self._val_cb)

    def _query(self, account: str, *extra: tuple[int, int]) -> tuple[int, list[int]]:
        k = self._k
        owned = [self._str(self.service), self._str(account)]
        pairs = [
            (k["kSecClass"], k["kSecClassGenericPassword"]),
            (k["kSecAttrService"], owned[0]),
            (k["kSecAttrAccount"], owned[1]),
            *extra,
        ]
        return self._dict(pairs), owned

    def _release(self, *refs: int | None) -> None:
        for ref in refs:
            if ref:
                self._cf.CFRelease(ref)

    # -- SecretStore -------------------------------------------------------

    def get(self, account: str) -> str | None:
        k = self._k
        query, owned = self._query(
            account,
            (k["kSecReturnData"], self._true),
            (k["kSecMatchLimit"], k["kSecMatchLimitOne"]),
        )
        out = ctypes.c_void_p()
        try:
            status = self._sec.SecItemCopyMatching(query, ctypes.byref(out))
            if status == _ERR_ITEM_NOT_FOUND:
                return None
            if status != 0:
                raise KeychainError("read", status)
            data = out.value
            length = self._cf.CFDataGetLength(data)
            return ctypes.string_at(self._cf.CFDataGetBytePtr(data), length).decode()
        finally:
            self._release(query, out.value, *owned)

    def set(self, account: str, secret: str) -> None:
        k = self._k
        raw = secret.encode()
        data = self._cf.CFDataCreate(None, raw, len(raw))
        extra = [(k["kSecValueData"], data)]
        if sys.platform == "ios":
            # Not synced to iCloud, not readable while locked.
            extra.append(
                (k["kSecAttrAccessible"], k["kSecAttrAccessibleWhenUnlockedThisDeviceOnly"])
            )
        query, owned = self._query(account, *extra)
        try:
            status = self._sec.SecItemAdd(query, None)
            if status == _ERR_DUPLICATE_ITEM:
                match, owned2 = self._query(account)
                update = self._dict([(k["kSecValueData"], data)])
                try:
                    status = self._sec.SecItemUpdate(match, update)
                finally:
                    self._release(match, update, *owned2)
            if status != 0:
                raise KeychainError("write", status)
        finally:
            self._release(query, data, *owned)

    def delete(self, account: str) -> None:
        query, owned = self._query(account)
        try:
            status = self._sec.SecItemDelete(query)
            if status not in (0, _ERR_ITEM_NOT_FOUND):
                raise KeychainError("delete", status)
        finally:
            self._release(query, *owned)


def default_secret_store() -> SecretStore:
    """The Keychain where it works, else memory (and say so)."""
    if sys.platform in ("darwin", "ios"):
        try:
            store = KeychainSecretStore()
            store.get("__probe__")  # surfaces entitlement errors (-34018) now
            return store
        except (OSError, KeychainError) as exc:
            log.warning("Keychain unavailable, secrets kept in memory only: %s", exc)
    return MemorySecretStore()
