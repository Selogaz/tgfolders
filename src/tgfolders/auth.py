"""Login, logout and the connected client.

Sign-in works with a code or, when the code does not arrive (common for new API apps), with a
QR code scanned from the Telegram app on your phone. Run it in a normal terminal: you type the
code and your 2FA password there.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from contextlib import asynccontextmanager

from tgfolders.session import StoreError, default_store

FLOOD_MAX_WAIT = 60   # seconds Telethon may sleep on FloodWait by itself
LOGIN_ATTEMPTS = 3
QR_WAIT = 30          # seconds per QR (a login token lives ~30 s)
QR_ROUNDS = 10        # ~5 minutes in total
QR_HINT = "No code? Try signing in with a QR code: tgfolders login --qr"
QR_HOWTO = ("On your phone: Telegram → Settings → Devices → Link Desktop Device → scan the QR. "
            "The QR refreshes every ~30 s.")
CODE_REMINDER = ("The code usually arrives in the Telegram app (the official 'Telegram' chat), not "
                 "by SMS. Never forward it: Telegram invalidates forwarded codes.")


NO_TTY = ("login needs an interactive terminal: you type the login code and 2FA password there. "
          "Run `tgfolders login` yourself in a normal terminal, not through a script or an AI "
          "agent (anything typed through an agent ends up in its transcript).")


def interactive() -> bool:
    """Both stdin and stdout are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


class TelegramError(RuntimeError):
    """Network/API/session problem."""


class NotLoggedIn(TelegramError):
    """No stored credentials."""


class LoginError(TelegramError):
    """Sign-in failed or was cancelled; the message is user-facing."""


def make_client(api_id: int, api_hash: str, session: str):
    """Telethon client on an in-memory StringSession (no .session file on disk)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session), api_id, api_hash, flood_sleep_threshold=FLOOD_MAX_WAIT)


@asynccontextmanager
async def connected(store=None):
    """Connected, authorized client from stored credentials."""
    creds = (store or default_store()).load()
    if creds is None:
        raise NotLoggedIn("not logged in: run `tgfolders login`")
    client = make_client(creds["api_id"], creds["api_hash"], creds["session"])
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramError("the stored session is no longer valid: run `tgfolders login` again")
        yield client
    finally:
        await client.disconnect()


# --- sign-in ------------------------------------------------------------------------

_NEXT_TYPES = {
    "CodeTypeSms": "SMS", "CodeTypeCall": "a call", "CodeTypeFlashCall": "a flash call",
    "CodeTypeMissedCall": "a missed call", "CodeTypeFragmentSms": "Fragment",
}


def describe_sent(sent) -> str:
    """Where Telegram sent the code (by sent.type), plus next_type and timeout."""
    st = getattr(sent, "type", None)
    name = type(st).__name__
    where = {
        "SentCodeTypeApp": "to the Telegram app on another device (the official 'Telegram' chat), not SMS",
        "SentCodeTypeSms": "by SMS",
        "SentCodeTypeCall": "by a phone call",
        "SentCodeTypeFlashCall": "by a phone call",
        "SentCodeTypeFragmentSms": "via Fragment (anonymous number)",
    }.get(name)
    if name == "SentCodeTypeMissedCall":
        where = f"by a missed call: the code is the last digits of the caller's number ({st.prefix}…)"
    elif name == "SentCodeTypeEmailCode":
        where = f"to your email {getattr(st, 'email_pattern', '')}"
    lines = [f"Code sent {where or f'via {name}'}."]
    nxt = getattr(sent, "next_type", None)
    if nxt is not None:
        how = _NEXT_TYPES.get(type(nxt).__name__, type(nxt).__name__)
        lines.append(f"You can request it again via {how}: press Enter or type r.")
    if getattr(sent, "timeout", None):
        lines.append(f"Resending is available in {sent.timeout} s.")
    return "\n".join(lines)


async def _resend(client, phone: str, sent):
    from telethon.tl import functions
    new = await client(functions.auth.ResendCodeRequest(phone_number=phone, phone_code_hash=sent.phone_code_hash))
    print(describe_sent(new))
    return new


async def _enter_password(client) -> None:
    from telethon.errors import PasswordHashInvalidError
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        try:
            await client.sign_in(password=getpass.getpass("2FA password: "))
            return
        except PasswordHashInvalidError:
            if attempt == LOGIN_ATTEMPTS:
                raise LoginError(f"wrong 2FA password {LOGIN_ATTEMPTS} times; giving up") from None
            print(f"Wrong 2FA password ({attempt}/{LOGIN_ATTEMPTS}).")


async def _enter_code(client, phone: str, sent) -> None:
    from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError
    wrong = 0
    while True:
        raw = input("Code (Enter or r to resend, q to quit): ").strip()
        if raw.lower() == "q":
            raise LoginError("login cancelled")
        if raw == "" or raw.lower() == "r":
            sent = await _resend(client, phone, sent)
            continue
        try:
            await client.sign_in(phone, raw, phone_code_hash=sent.phone_code_hash)
            return
        except SessionPasswordNeededError:
            await _enter_password(client)
            return
        except PhoneCodeInvalidError:
            wrong += 1
            if wrong >= LOGIN_ATTEMPTS:
                raise LoginError(f"wrong code {LOGIN_ATTEMPTS} times; giving up. {QR_HINT}") from None
            print(f"Wrong code ({wrong}/{LOGIN_ATTEMPTS}): make sure it is the latest one.")
        except PhoneCodeExpiredError:
            print("The code expired. Sending a new one.")
            sent = await _resend(client, phone, sent)


def _show_qr(url: str, invert: bool) -> None:
    """ASCII QR in the terminal. The URL itself is never printed: it is an account login token."""
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=invert)


async def _qr_login(client, invert: bool) -> None:
    from telethon.errors import SessionPasswordNeededError
    qr = await client.qr_login()
    print(QR_HOWTO)
    for _ in range(QR_ROUNDS):
        _show_qr(qr.url, invert)
        try:
            await qr.wait(QR_WAIT)
            return
        except SessionPasswordNeededError:
            await _enter_password(client)
            return
        except asyncio.TimeoutError:
            print("The QR expired, drawing a new one...")
            await qr.recreate()
    raise LoginError("the QR was not scanned within ~5 minutes; giving up")


async def login(qr: bool = False, invert: bool = True, store=None) -> str:
    from telethon.errors import ApiIdInvalidError, FloodWaitError, PhoneNumberInvalidError
    if not interactive():
        raise LoginError(NO_TTY)
    store = store or default_store()
    print("Get api_id and api_hash at https://my.telegram.org → API development tools")
    try:
        api_id = int(input("api_id: ").strip())
    except ValueError:
        raise LoginError("api_id must be a number") from None
    api_hash = getpass.getpass("api_hash: ").strip()
    phone = "" if qr else input("Phone number (+...): ").strip()
    client = make_client(api_id, api_hash, "")
    try:
        await client.connect()
        if qr:
            await _qr_login(client, invert)
        else:
            print(CODE_REMINDER)
            print(QR_HINT)
            sent = await client.send_code_request(phone)
            print(describe_sent(sent))
            await _enter_code(client, phone, sent)
        me = await client.get_me()
        store.save({"api_id": api_id, "api_hash": api_hash, "session": client.session.save()})
        print(f"Credentials saved to {store.where}.")
        return " ".join(x for x in (me.first_name, me.last_name) if x) or me.username or str(me.id)
    except PhoneNumberInvalidError:
        raise LoginError("invalid phone number: use +<country code><number>") from None
    except ApiIdInvalidError:
        raise LoginError("invalid api_id/api_hash: check them at https://my.telegram.org") from None
    except FloodWaitError as e:
        raise LoginError(f"Telegram asks to wait {e.seconds} s before trying again") from None
    finally:
        await client.disconnect()


async def logout(force: bool = False, store=None) -> str:
    """Terminate the session on the server, then delete local credentials."""
    store = store or default_store()
    creds = store.load()
    if creds is None:
        return "not logged in"
    client = make_client(creds["api_id"], creds["api_hash"], creds["session"])
    try:
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()
    except Exception as e:  # noqa: BLE001 — network/API
        if not force:
            raise TelegramError(
                f"server logout failed ({e.__class__.__name__}); credentials kept. Retry, or use "
                "--force to delete them locally and end the session in Telegram → Settings → Devices"
            ) from None
    finally:
        await client.disconnect()
    store.delete()
    return "logged out, credentials deleted"


__all__ = ["StoreError", "TelegramError", "NotLoggedIn", "LoginError", "connected", "login", "logout",
           "make_client", "describe_sent"]
