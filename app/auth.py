"""Single-user login: scrypt password hash + stateless HMAC-signed session cookie. Standard library only.

Enabled when APP_PASSWORD_HASH is set. Create one with:  python -m app.auth
Optional SESSION_SECRET keeps sessions across restarts (otherwise a random one is used per process).
"""
import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys
import time

COOKIE = "wft_session"
SESSION_TTL = 7 * 86400
N, R, P = 2**14, 8, 1
MAX_FAILURES, FAILURE_WINDOW = 5, 300

_random_secret = secrets.token_bytes(32)
_failures: dict[str, list[float]] = {}


def enabled() -> bool:
    return bool(os.environ.get("APP_PASSWORD_HASH", "").strip())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """`scrypt:N:r:p:salt:hash`. Colons only (no `$`), so it is safe in a docker env_file."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt:{N}:{R}:{P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.strip().split(":")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


def check_password(password: str) -> bool:
    return enabled() and verify_password(password, os.environ["APP_PASSWORD_HASH"])


def _key() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "")
    return secret.encode() if secret else _random_secret


def _sign(payload: str) -> str:
    return _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())


def make_session(now: float | None = None) -> str:
    payload = f"{int((now or time.time()) + SESSION_TTL)}.{secrets.token_urlsafe(8)}"
    return f"{payload}.{_sign(payload)}"


def valid_session(cookie: str | None, now: float | None = None) -> bool:
    if not cookie:
        return False
    try:
        expires, nonce, sig = cookie.split(".")
        return hmac.compare_digest(sig, _sign(f"{expires}.{nonce}")) and int(expires) > (now or time.time())
    except ValueError:
        return False


def throttled(client: str, now: float | None = None) -> bool:
    now = now or time.time()
    recent = [t for t in _failures.get(client, []) if now - t < FAILURE_WINDOW]
    _failures[client] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(client: str) -> None:
    _failures.setdefault(client, []).append(time.time())


def clear_failures(client: str) -> None:
    _failures.pop(client, None)


if __name__ == "__main__":
    pw = getpass.getpass("New app password (12+ chars): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords differ or are shorter than 12 characters")
    print(f"APP_PASSWORD_HASH={hash_password(pw)}")
    print(f"SESSION_SECRET={secrets.token_hex(32)}")
