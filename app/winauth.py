"""Встроенная проверка подлинности Windows (NTLM / Kerberos) без сторонних библиотек.

Браузер сам передает учетную запись, под которой пользователь вошел в Windows
(в том числе доменную учетную запись Active Directory), по протоколу HTTP Negotiate:

    браузер  → GET /api/login/windows
    сервер   ← 401, WWW-Authenticate: Negotiate (и NTLM)
    браузер  → Authorization: Negotiate <токен Kerberos или NTLM>
    сервер   ← 401, WWW-Authenticate: Negotiate <ответ>   (только для NTLM, 2-й шаг)
    браузер  → Authorization: Negotiate <токен>
    сервер   ← 200, имя пользователя ДОМЕН\\логин

Токены проверяет сама Windows через SSPI (secur32.dll) — модуль вызывает ее
через ctypes, поэтому pywin32 и другие пакеты не нужны (работает и в офлайн-установке).
Пароль пользователя сервис не видит и не хранит.
"""
import ctypes
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

SEC_E_OK = 0x00000000
SEC_I_CONTINUE_NEEDED = 0x00090312
SEC_I_COMPLETE_NEEDED = 0x00090313
SEC_I_COMPLETE_AND_CONTINUE = 0x00090314
SECPKG_CRED_INBOUND = 1
SECURITY_NATIVE_DREP = 0x10
SECBUFFER_TOKEN = 2
SECBUFFER_VERSION = 0
SECPKG_ATTR_NAMES = 1
NAME_SAM_COMPATIBLE = 2   # ДОМЕН\логин
NAME_DISPLAY = 3          # отображаемое имя (ФИО из AD)
MAX_TOKEN = 64 * 1024
CONTEXT_TTL = 120         # сек. на незавершенное рукопожатие NTLM


class AuthResult:
    """Итог одного шага: out_token — что отправить браузеру, user — если вход завершен."""

    def __init__(self, out_token=b"", user=None, failed=False):
        self.out_token = out_token
        self.user = user
        self.failed = failed


def is_initial_token(token):
    """Первое сообщение рукопожатия: SPNEGO (0x60) или NTLM Type 1."""
    return token[:1] == b"\x60" or token[:12] == b"NTLMSSP\x00\x01\x00\x00\x00"


if os.name == "nt":
    from ctypes import wintypes

    class SecHandle(ctypes.Structure):
        _fields_ = [("dwLower", ctypes.c_void_p), ("dwUpper", ctypes.c_void_p)]

    class SecBuffer(ctypes.Structure):
        _fields_ = [("cbBuffer", wintypes.ULONG), ("BufferType", wintypes.ULONG),
                    ("pvBuffer", ctypes.c_void_p)]

    class SecBufferDesc(ctypes.Structure):
        _fields_ = [("ulVersion", wintypes.ULONG), ("cBuffers", wintypes.ULONG),
                    ("pBuffers", ctypes.POINTER(SecBuffer))]

    class SecPkgContextNames(ctypes.Structure):
        _fields_ = [("sUserName", ctypes.c_void_p)]

    _secur32 = ctypes.WinDLL("secur32")
    _PH = ctypes.POINTER(SecHandle)
    _secur32.AcquireCredentialsHandleW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, _PH, ctypes.POINTER(ctypes.c_longlong)]
    _secur32.AcquireCredentialsHandleW.restype = ctypes.c_long
    _secur32.AcceptSecurityContext.argtypes = [
        _PH, _PH, ctypes.POINTER(SecBufferDesc), wintypes.ULONG, wintypes.ULONG, _PH,
        ctypes.POINTER(SecBufferDesc), ctypes.POINTER(wintypes.ULONG),
        ctypes.POINTER(ctypes.c_longlong)]
    _secur32.AcceptSecurityContext.restype = ctypes.c_long
    _secur32.CompleteAuthToken.argtypes = [_PH, ctypes.POINTER(SecBufferDesc)]
    _secur32.CompleteAuthToken.restype = ctypes.c_long
    _secur32.QueryContextAttributesW.argtypes = [_PH, wintypes.ULONG, ctypes.c_void_p]
    _secur32.QueryContextAttributesW.restype = ctypes.c_long
    _secur32.DeleteSecurityContext.argtypes = [_PH]
    _secur32.DeleteSecurityContext.restype = ctypes.c_long
    _secur32.FreeContextBuffer.argtypes = [ctypes.c_void_p]
    _secur32.FreeContextBuffer.restype = ctypes.c_long
    _secur32.GetUserNameExW.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    _secur32.GetUserNameExW.restype = wintypes.BOOLEAN
    _secur32.TranslateNameW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int,
                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    _secur32.TranslateNameW.restype = wintypes.BOOLEAN


def _status(code):
    return code & 0xFFFFFFFF


class SspiAuthenticator:
    """Хранит незавершенные рукопожатия по ключу соединения (IP:порт клиента)."""

    available = os.name == "nt"

    def __init__(self):
        if not self.available:
            raise RuntimeError("SSPI доступен только в Windows")
        self._creds = {}
        self._contexts = {}   # key -> (SecHandle, время начала)
        self._lock = threading.Lock()
        for package in ("Negotiate", "NTLM"):
            cred, expiry = SecHandle(), ctypes.c_longlong()
            rc = _secur32.AcquireCredentialsHandleW(
                None, package, SECPKG_CRED_INBOUND, None, None, None, None,
                ctypes.byref(cred), ctypes.byref(expiry))
            if _status(rc) != SEC_E_OK:
                raise OSError(f"AcquireCredentialsHandle({package}) вернул 0x{_status(rc):08X}")
            self._creds[package] = cred

    def _drop(self, key):
        item = self._contexts.pop(key, None)
        if item:
            _secur32.DeleteSecurityContext(ctypes.byref(item[0]))

    def _cleanup(self):
        now = time.monotonic()
        for key in [k for k, (_, started) in self._contexts.items() if now - started > CONTEXT_TTL]:
            self._drop(key)

    def step(self, key, scheme, token):
        package = "NTLM" if scheme.lower() == "ntlm" else "Negotiate"
        with self._lock:
            self._cleanup()
            if is_initial_token(token):
                self._drop(key)
            existing = self._contexts.get(key)
            ctx = existing[0] if existing else SecHandle()
            started = existing[1] if existing else time.monotonic()

            in_buf = ctypes.create_string_buffer(token, len(token))
            in_sec = SecBuffer(len(token), SECBUFFER_TOKEN, ctypes.cast(in_buf, ctypes.c_void_p))
            in_desc = SecBufferDesc(SECBUFFER_VERSION, 1, ctypes.pointer(in_sec))
            out_buf = ctypes.create_string_buffer(MAX_TOKEN)
            out_sec = SecBuffer(MAX_TOKEN, SECBUFFER_TOKEN, ctypes.cast(out_buf, ctypes.c_void_p))
            out_desc = SecBufferDesc(SECBUFFER_VERSION, 1, ctypes.pointer(out_sec))
            attrs, expiry = wintypes.ULONG(), ctypes.c_longlong()

            rc = _status(_secur32.AcceptSecurityContext(
                ctypes.byref(self._creds[package]), ctypes.byref(ctx) if existing else None,
                ctypes.byref(in_desc), 0, SECURITY_NATIVE_DREP, ctypes.byref(ctx),
                ctypes.byref(out_desc), ctypes.byref(attrs), ctypes.byref(expiry)))
            if rc in (SEC_I_COMPLETE_NEEDED, SEC_I_COMPLETE_AND_CONTINUE):
                _secur32.CompleteAuthToken(ctypes.byref(ctx), ctypes.byref(out_desc))
                rc = SEC_E_OK if rc == SEC_I_COMPLETE_NEEDED else SEC_I_CONTINUE_NEEDED
            out_token = out_buf.raw[:out_sec.cbBuffer]

            if rc == SEC_I_CONTINUE_NEEDED:
                self._contexts[key] = (ctx, started)
                return AuthResult(out_token)
            if rc != SEC_E_OK:
                log.warning("Windows-вход отклонен для %s: AcceptSecurityContext 0x%08X", key, rc)
                if existing:
                    self._contexts.pop(key, None)
                    _secur32.DeleteSecurityContext(ctypes.byref(ctx))
                return AuthResult(failed=True)

            self._contexts.pop(key, None)
            try:
                user = self._context_user(ctx)
            finally:
                _secur32.DeleteSecurityContext(ctypes.byref(ctx))
            return AuthResult(out_token, user=user, failed=not user)

    @staticmethod
    def _context_user(ctx):
        names = SecPkgContextNames()
        rc = _status(_secur32.QueryContextAttributesW(
            ctypes.byref(ctx), SECPKG_ATTR_NAMES, ctypes.byref(names)))
        if rc != SEC_E_OK or not names.sUserName:
            log.warning("QueryContextAttributes(NAMES) вернул 0x%08X", rc)
            return None
        try:
            return ctypes.wstring_at(names.sUserName)
        finally:
            _secur32.FreeContextBuffer(names.sUserName)


def display_name(login):
    """ФИО из Active Directory для ДОМЕН\\логин (или None, если узнать нельзя)."""
    if os.name != "nt" or not login or "\\" not in login:
        return None
    size = wintypes.ULONG(512)
    buf = ctypes.create_unicode_buffer(size.value)
    try:
        if _secur32.TranslateNameW(login, NAME_SAM_COMPATIBLE, NAME_DISPLAY, buf, ctypes.byref(size)):
            return buf.value.strip() or None
    except OSError:
        pass
    return None


def process_user():
    """Учетная запись, под которой запущен сам сервис: (ДОМЕН\\логин, ФИО или None)."""
    if os.name != "nt":
        import getpass
        return getpass.getuser(), None

    def query(fmt):
        size = wintypes.ULONG(512)
        buf = ctypes.create_unicode_buffer(size.value)
        if _secur32.GetUserNameExW(fmt, buf, ctypes.byref(size)):
            return buf.value.strip() or None
        return None

    login = query(NAME_SAM_COMPATIBLE)
    if not login:
        import getpass
        domain = os.environ.get("USERDOMAIN")
        login = f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()
    return login, query(NAME_DISPLAY)  # NameDisplay работает только в домене
