"""Credential storage without secrets in subprocess arguments.

The production implementation calls Apple's Security.framework directly via
``ctypes``.  It intentionally does not shell out to the ``security`` command:
doing so commonly places a password in argv where other local processes may
observe it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


ERR_SEC_SUCCESS = 0
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300
_CF_STRING_ENCODING_UTF8 = 0x08000100


class CredentialStoreError(RuntimeError):
    """Raised when a credential backend cannot complete an operation."""


@runtime_checkable
class CredentialStore(Protocol):
    """Minimal secret-store contract.

    Implementations must not log secret values or place them in command-line
    arguments.  Service and account identifiers are non-secret metadata.
    """

    def get(self, service: str, account: str) -> str | None:
        """Return a secret or ``None`` when no item exists."""

    def set(self, service: str, account: str, secret: str) -> None:
        """Create or replace a secret."""

    def delete(self, service: str, account: str) -> bool:
        """Delete an item, returning whether an item existed."""


@dataclass
class InMemoryCredentialStore:
    """Non-persistent fake for unit tests and local dry-run simulations."""

    _items: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def get(self, service: str, account: str) -> str | None:
        _validate_key(service, account)
        return self._items.get((service, account))

    def set(self, service: str, account: str, secret: str) -> None:
        _validate_item(service, account, secret)
        self._items[(service, account)] = secret

    def delete(self, service: str, account: str) -> bool:
        _validate_key(service, account)
        return self._items.pop((service, account), None) is not None


class _CFDictionaryKeyCallBacks(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_long),
        ("retain", ctypes.c_void_p),
        ("release", ctypes.c_void_p),
        ("copy_description", ctypes.c_void_p),
        ("equal", ctypes.c_void_p),
        ("hash", ctypes.c_void_p),
    ]


class _CFDictionaryValueCallBacks(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_long),
        ("retain", ctypes.c_void_p),
        ("release", ctypes.c_void_p),
        ("copy_description", ctypes.c_void_p),
        ("equal", ctypes.c_void_p),
    ]


class _SecurityFramework:
    """Small, private wrapper around the generic-password Security APIs."""

    def __init__(self) -> None:
        if platform.system() != "Darwin":
            raise CredentialStoreError("macOS Security.framework is only available on macOS")

        security_path = ctypes.util.find_library("Security")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        if not security_path or not core_foundation_path:
            raise CredentialStoreError("Apple Security.framework could not be loaded")

        try:
            self.security = ctypes.CDLL(security_path)
            self.cf = ctypes.CDLL(core_foundation_path)
        except OSError as exc:  # pragma: no cover - depends on the host runtime
            raise CredentialStoreError("Apple Security.framework could not be loaded") from exc

        self._configure_functions()
        self.key_callbacks = _CFDictionaryKeyCallBacks.in_dll(
            self.cf, "kCFTypeDictionaryKeyCallBacks"
        )
        self.value_callbacks = _CFDictionaryValueCallBacks.in_dll(
            self.cf, "kCFTypeDictionaryValueCallBacks"
        )

        self.k_sec_class = self._constant(self.security, "kSecClass")
        self.k_sec_class_generic_password = self._constant(
            self.security, "kSecClassGenericPassword"
        )
        self.k_sec_attr_service = self._constant(self.security, "kSecAttrService")
        self.k_sec_attr_account = self._constant(self.security, "kSecAttrAccount")
        self.k_sec_value_data = self._constant(self.security, "kSecValueData")
        self.k_sec_return_data = self._constant(self.security, "kSecReturnData")
        self.k_sec_match_limit = self._constant(self.security, "kSecMatchLimit")
        self.k_sec_match_limit_one = self._constant(self.security, "kSecMatchLimitOne")
        self.k_cf_boolean_true = self._constant(self.cf, "kCFBooleanTrue")

    @staticmethod
    def _constant(library: ctypes.CDLL, name: str) -> int:
        value = ctypes.c_void_p.in_dll(library, name).value
        if not value:  # pragma: no cover - defensive against a broken framework
            raise CredentialStoreError(f"Security framework constant is unavailable: {name}")
        return value

    def _configure_functions(self) -> None:
        self.cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFDataCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_long,
        ]
        self.cf.CFDataCreate.restype = ctypes.c_void_p
        self.cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self.cf.CFDataGetLength.restype = ctypes.c_long
        self.cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self.cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
        self.cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.POINTER(_CFDictionaryKeyCallBacks),
            ctypes.POINTER(_CFDictionaryValueCallBacks),
        ]
        self.cf.CFDictionaryCreate.restype = ctypes.c_void_p
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.restype = None

        self.security.SecItemCopyMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.security.SecItemCopyMatching.restype = ctypes.c_int32
        self.security.SecItemAdd.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.security.SecItemAdd.restype = ctypes.c_int32
        self.security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.SecItemUpdate.restype = ctypes.c_int32
        self.security.SecItemDelete.argtypes = [ctypes.c_void_p]
        self.security.SecItemDelete.restype = ctypes.c_int32

    def string(self, value: str) -> int:
        result = self.cf.CFStringCreateWithCString(
            None, value.encode("utf-8"), _CF_STRING_ENCODING_UTF8
        )
        if not result:
            raise CredentialStoreError("Could not allocate Keychain metadata")
        return result

    def data(self, value: bytes) -> int:
        raw = (ctypes.c_uint8 * len(value)).from_buffer_copy(value)
        result = self.cf.CFDataCreate(None, raw, len(value))
        if not result:
            raise CredentialStoreError("Could not allocate secret data")
        return result

    def dictionary(self, items: list[tuple[int, int]]) -> int:
        keys = (ctypes.c_void_p * len(items))(*(key for key, _ in items))
        values = (ctypes.c_void_p * len(items))(*(value for _, value in items))
        result = self.cf.CFDictionaryCreate(
            None,
            keys,
            values,
            len(items),
            ctypes.byref(self.key_callbacks),
            ctypes.byref(self.value_callbacks),
        )
        if not result:
            raise CredentialStoreError("Could not build a Keychain request")
        return result

    def release(self, *objects: int | None) -> None:
        for item in objects:
            if item:
                self.cf.CFRelease(item)

    def read_data(self, data_ref: int) -> bytes:
        length = self.cf.CFDataGetLength(data_ref)
        pointer = self.cf.CFDataGetBytePtr(data_ref)
        return ctypes.string_at(pointer, length)


class MacOSSecurityCredentialStore:
    """Store generic passwords through Apple's Security.framework.

    The framework is loaded lazily so importing this module remains safe on
    Linux CI hosts.  A framework object may be injected for focused tests.
    """

    def __init__(self, framework: _SecurityFramework | None = None) -> None:
        self._framework = framework

    @property
    def framework(self) -> _SecurityFramework:
        if self._framework is None:
            self._framework = _SecurityFramework()
        return self._framework

    def get(self, service: str, account: str) -> str | None:
        _validate_key(service, account)
        fw = self.framework
        service_ref = account_ref = query = result_ref = None
        try:
            service_ref = fw.string(service)
            account_ref = fw.string(account)
            query = fw.dictionary([
                (fw.k_sec_class, fw.k_sec_class_generic_password),
                (fw.k_sec_attr_service, service_ref),
                (fw.k_sec_attr_account, account_ref),
                (fw.k_sec_return_data, fw.k_cf_boolean_true),
                (fw.k_sec_match_limit, fw.k_sec_match_limit_one),
            ])
            result = ctypes.c_void_p()
            status = fw.security.SecItemCopyMatching(query, ctypes.byref(result))
            if status == ERR_SEC_ITEM_NOT_FOUND:
                return None
            _raise_for_status(status, "Keychain lookup")
            result_ref = result.value
            try:
                return fw.read_data(result_ref).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CredentialStoreError("Stored Keychain item is not UTF-8") from exc
        finally:
            fw.release(result_ref, query, service_ref, account_ref)

    def set(self, service: str, account: str, secret: str) -> None:
        _validate_item(service, account, secret)
        fw = self.framework
        service_ref = account_ref = secret_ref = query = update = add = None
        try:
            service_ref = fw.string(service)
            account_ref = fw.string(account)
            secret_ref = fw.data(secret.encode("utf-8"))
            query = fw.dictionary([
                (fw.k_sec_class, fw.k_sec_class_generic_password),
                (fw.k_sec_attr_service, service_ref),
                (fw.k_sec_attr_account, account_ref),
            ])
            update = fw.dictionary([(fw.k_sec_value_data, secret_ref)])
            status = fw.security.SecItemUpdate(query, update)
            if status == ERR_SEC_ITEM_NOT_FOUND:
                add = fw.dictionary([
                    (fw.k_sec_class, fw.k_sec_class_generic_password),
                    (fw.k_sec_attr_service, service_ref),
                    (fw.k_sec_attr_account, account_ref),
                    (fw.k_sec_value_data, secret_ref),
                ])
                status = fw.security.SecItemAdd(add, None)
                if status == ERR_SEC_DUPLICATE_ITEM:
                    status = fw.security.SecItemUpdate(query, update)
            _raise_for_status(status, "Keychain update")
        finally:
            fw.release(add, update, query, secret_ref, service_ref, account_ref)

    def delete(self, service: str, account: str) -> bool:
        _validate_key(service, account)
        fw = self.framework
        service_ref = account_ref = query = None
        try:
            service_ref = fw.string(service)
            account_ref = fw.string(account)
            query = fw.dictionary([
                (fw.k_sec_class, fw.k_sec_class_generic_password),
                (fw.k_sec_attr_service, service_ref),
                (fw.k_sec_attr_account, account_ref),
            ])
            status = fw.security.SecItemDelete(query)
            if status == ERR_SEC_ITEM_NOT_FOUND:
                return False
            _raise_for_status(status, "Keychain delete")
            return True
        finally:
            fw.release(query, service_ref, account_ref)


def _validate_key(service: str, account: str) -> None:
    if not service or not account:
        raise ValueError("service and account are required")
    if "\x00" in service or "\x00" in account:
        raise ValueError("service and account cannot contain null bytes")


def _validate_item(service: str, account: str, secret: str) -> None:
    _validate_key(service, account)
    if not secret:
        raise ValueError("secret is required")


def _raise_for_status(status: int, operation: str) -> None:
    if status != ERR_SEC_SUCCESS:
        # Do not include service, account, or secret values in error messages.
        raise CredentialStoreError(f"{operation} failed with OSStatus {status}")
