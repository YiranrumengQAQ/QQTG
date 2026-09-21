"""Password hashing, secret encryption and log redaction."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


class SecretBox:
    """Fernet wrapper keyed from the bootstrap secret key."""

    def __init__(self, secret_key: str):
        if not secret_key:
            raise ValueError("QQTG_SECRET_KEY is not configured")
        key = hashlib.sha256(("qqtg-secretbox:" + secret_key).encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("cannot decrypt secret: wrong QQTG_SECRET_KEY?") from exc


_TG_TOKEN_RE = re.compile(r"\b(\d{6,12}):([A-Za-z0-9_-]{30,})\b")
_TG_FILE_URL_RE = re.compile(r"(/file/bot)(\d+):([A-Za-z0-9_-]+)")
_ACCESS_TOKEN_RE = re.compile(r"(access_token=)([^&\s\"']+)", re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})")


def sign_media_token(secret_key: str, rel_path: str, expires: int, name: str = "") -> str:
    """HMAC signature for a temporary /qqbot/media download link."""
    msg = f"{rel_path}|{expires}|{name}"
    key = hashlib.sha256(("qqtg-media:" + secret_key).encode("utf-8")).digest()
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_media_token(secret_key: str, rel_path: str, expires: int, name: str, token: str) -> bool:
    if expires < time.time():
        return False
    expected = sign_media_token(secret_key, rel_path, expires, name)
    return hmac.compare_digest(expected, token or "")


def redact(text: str) -> str:
    """Strip anything that looks like a credential from log lines."""
    if not text:
        return text
    text = _TG_FILE_URL_RE.sub(r"\1\2:***", text)
    text = _TG_TOKEN_RE.sub(lambda m: m.group(1) + ":***", text)
    text = _ACCESS_TOKEN_RE.sub(r"\1***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    return text


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * 8
    return "•" * 12 + value[-keep:]
