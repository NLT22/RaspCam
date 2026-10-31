"""Password hashing (scrypt) and a CLI that prints values for config.yaml."""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys

N, R, P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest, validate=True)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt, validate=True),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def main() -> int:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match", file=sys.stderr)
        return 1
    print(f'  password_hash: "{hash_password(password)}"')
    print(f'  secret_key: "{secrets.token_hex(32)}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
