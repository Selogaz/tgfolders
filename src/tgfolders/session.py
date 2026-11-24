"""Credentials storage: api_id, api_hash and a Telethon StringSession.

A session string is full access to your Telegram account. By default it lives in the system
keyring (Secret Service / macOS Keychain / Windows Credential Locker via the ``keyring``
package). If no keyring is available, or ``TGFOLDERS_SESSION_FILE`` is set, it is stored in a
JSON file readable only by you (mode 0600).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE = "tgfolders"
KEY = "credentials"


class StoreError(RuntimeError):
    """The credentials store cannot be read or written."""


def config_dir() -> Path:
    base = os.environ.get("TGFOLDERS_CONFIG_DIR")
    if base:
        return Path(base).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "tgfolders"


def _validate(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    try:
        return {"api_id": int(d["api_id"]), "api_hash": str(d["api_hash"]), "session": str(d["session"])}
    except (KeyError, TypeError, ValueError):
        return None


class FileStore:
    def __init__(self, path: Path):
        self.path = path

    @property
    def where(self) -> str:
        return f"file {self.path}"

    def load(self) -> dict | None:
        try:
            return _validate(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            raise StoreError(f"cannot read {self.path}: {e}") from None

    def save(self, creds: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(creds, f)
        os.replace(tmp, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class KeyringStore:
    def __init__(self, kr):
        self.kr = kr

    @property
    def where(self) -> str:
        return f"system keyring ({type(self.kr).__name__})"

    def load(self) -> dict | None:
        try:
            raw = self.kr.get_password(SERVICE, KEY)
        except Exception as e:  # noqa: BLE001 — keyring backends raise various errors
            raise StoreError(f"keyring is unavailable: {e.__class__.__name__}") from None
        if raw is None:
            return None
        try:
            return _validate(json.loads(raw))
        except json.JSONDecodeError:
            return None

    def save(self, creds: dict) -> None:
        try:
            self.kr.set_password(SERVICE, KEY, json.dumps(creds))
        except Exception as e:  # noqa: BLE001
            raise StoreError(f"cannot write to keyring: {e.__class__.__name__}") from None
        if self.load() != creds:
            raise StoreError("keyring did not keep the credentials (read-back mismatch)")

    def delete(self) -> None:
        try:
            self.kr.delete_password(SERVICE, KEY)
        except Exception:  # noqa: BLE001 — nothing to delete is fine
            pass


def default_store():
    env = os.environ.get("TGFOLDERS_SESSION_FILE")
    if env:
        return FileStore(Path(env).expanduser())
    try:
        import keyring
        kr = keyring.get_keyring()
        # fail.Keyring / null.Keyring have priority <= 0: no real backend installed
        if getattr(kr, "priority", 0) > 0:
            return KeyringStore(kr)
    except Exception:  # noqa: BLE001 — no usable keyring: fall back to a file
        pass
    return FileStore(config_dir() / "session.json")
