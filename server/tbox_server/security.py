"""Password hashing, JWT (HS256), login rate limiting, gateway tokens, secret sealing."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

PBKDF2_ITERATIONS = 240_000


class AuthError(Exception):
    pass


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, _b64(salt), _b64(dk))


_DUMMY = None


def verify_password(password, stored):
    global _DUMMY
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(it))
        return hmac.compare_digest(calc, _unb64(dk))
    except Exception:
        return False


def burn_time(password):
    """Spend the same time as a real check so unknown usernames are not distinguishable."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("dummy-password")
    verify_password(password, _DUMMY)


def check_password_policy(pw):
    if len(pw) < 10:
        return "Password must be at least 10 characters."
    if pw.lower() == pw or pw.upper() == pw or not any(c.isdigit() for c in pw):
        return "Password must mix upper and lower case letters and include a digit."
    return None


def jwt_encode(payload, key):
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(key.encode(), ("%s.%s" % (head, body)).encode(), hashlib.sha256).digest()
    return "%s.%s.%s" % (head, body, _b64(sig))


def jwt_decode(token, key):
    try:
        head, body, sig = token.split(".")
        if json.loads(_unb64(head)).get("alg") != "HS256":
            raise AuthError("bad algorithm")
        calc = hmac.new(key.encode(), ("%s.%s" % (head, body)).encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(calc, _unb64(sig)):
            raise AuthError("bad signature")
        payload = json.loads(_unb64(body))
    except AuthError:
        raise
    except Exception:
        raise AuthError("malformed token")
    if payload.get("exp", 0) < time.time():
        raise AuthError("token expired")
    return payload


def new_gateway_token():
    return "tbx_" + secrets.token_urlsafe(32)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    def __init__(self, max_failures=5, window_s=300):
        self.max, self.window = max_failures, window_s
        self._fails = {}
        self._lock = threading.Lock()

    def _prune(self, key, now):
        self._fails[key] = [t for t in self._fails.get(key, []) if now - t < self.window]

    def blocked(self, key):
        now = time.time()
        with self._lock:
            self._prune(key, now)
            return len(self._fails[key]) >= self.max

    def fail(self, key):
        now = time.time()
        with self._lock:
            self._prune(key, now)
            self._fails[key].append(now)

    def reset(self, key):
        with self._lock:
            self._fails.pop(key, None)


class Vault:
    """Seals device credentials. Uses Fernet (AES) when `cryptography` is installed,
    otherwise stores them base64-wrapped (NOT encrypted) and reports that in /api/system."""

    def __init__(self, secret):
        self.encrypted = False
        self._f = None
        try:
            from cryptography.fernet import Fernet
            key = base64.urlsafe_b64encode(hashlib.sha256(("vault:" + secret).encode()).digest())
            self._f = Fernet(key)
            self.encrypted = True
        except Exception:
            pass

    def seal(self, obj):
        raw = json.dumps(obj).encode()
        if self._f:
            return "f1:" + self._f.encrypt(raw).decode()
        return "p0:" + base64.b64encode(raw).decode()

    def unseal(self, text):
        if text.startswith("f1:"):
            if not self._f:
                raise ValueError("secret was encrypted but the cryptography package is missing")
            return json.loads(self._f.decrypt(text[3:].encode()))
        return json.loads(base64.b64decode(text[3:]))
