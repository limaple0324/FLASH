"""Process-lifetime opaque identities.

The secret is created by the operating-system CSPRNG on every process start. It
is never serialized, logged, or exposed. Raw command/account text is accepted
only as a call-local input and only the keyed digest is returned.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets


_SESSION_SECRET = os.urandom(32)


def session_hmac(namespace: str, *parts: str) -> str:
    digest = hmac.new(_SESSION_SECRET, digestmod=hashlib.sha256)
    for value in (namespace, *parts):
        encoded = str(value or "").encode("utf-8", "surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def process_identity(executable: str, command_line: str) -> str:
    normalized_executable = os.path.normcase(os.path.abspath(str(executable or ""))).casefold()
    normalized_command = " ".join(str(command_line or "").split()).casefold()
    if not normalized_executable or not normalized_command:
        return ""
    return session_hmac("process-command", normalized_executable, normalized_command)


def account_launch_identity(command_text: str) -> str:
    """Return an opaque account identity without returning parsed credentials."""
    raw_text = str(command_text or "")
    user_match = re.search(r"(?:[?&])user=([^&\s\"']+)", raw_text, re.IGNORECASE)
    password_match = re.search(r"(?:[?&])pass=([^&\s\"']+)", raw_text, re.IGNORECASE)
    username = user_match.group(1) if user_match else ""
    password = password_match.group(1) if password_match else ""
    if not username:
        return ""
    return session_hmac("account-launch", username.casefold(), password)


def new_opaque_id() -> str:
    """A disk-safe correlation ID unrelated to credentials or command text."""
    return secrets.token_urlsafe(24)
