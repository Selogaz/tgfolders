"""Command line: tgfolders {login,logout,status,init,show,plan,apply}."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tgfolders import __version__, auth, folders
from tgfolders.session import StoreError, config_dir, default_store


UNATTENDED = ("note: applying without confirmation in a non-interactive run. If an AI agent "
              "started this, the plan above was not reviewed by a person.")


def config_path(arg: Path | None) -> Path:
    return arg or config_dir() / "config.json"


def _load_cfg_or_empty(path: Path) -> folders.Config:
    try:
        return folders.load_config(path)
    except folders.ConfigError:
        return folders.Config(protected=[], folders=[])


async def _show(path: Path) -> None:
    cfg = _load_cfg_or_empty(path)
    async with auth.connected() as client:
        limits = await folders.read_limits(client)
        filters, chats = await folders.read_state(client)
    print(folders.format_show(filters, chats, cfg, limits))


async def _init(path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    async with auth.connected() as client:
        filters, _ = await folders.read_state(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(folders.init_config(filters), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"Wrote {path}. All {len(filters)} existing folders are protected; edit the file to "
          f"add rules, then run `tgfolders plan`.")
    return 0


async def _plan(path: Path, apply: bool, yes: bool, quiet: bool) -> int:
    cfg = folders.load_config(path)
    async with auth.connected() as client:
        limits = await folders.read_limits(client)
        filters, chats = await folders.read_state(client)
        plan = folders.build_plan(filters, chats, cfg, limits)
        print(folders.format_plan(plan, verbose_unmatched=not quiet))
        if plan.errors:
            return 1
        if not apply:
            return 0
        if not plan.n_ops:
            print("nothing to do")
            return 0
        if yes and not auth.interactive():
            print(UNATTENDED, file=sys.stderr)
        if not yes:
            ans = await asyncio.to_thread(input, f"Apply {plan.n_ops} folder operation(s)? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 0
        n = await folders.write(client, plan, cfg)
    print(f"done: {n} folder operation(s) applied")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tgfolders",
        description="Sort your Telegram groups and channels into chat folders by keyword rules.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--config", type=Path, help="config file (default: ~/.config/tgfolders/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="sign in (code or --qr); credentials go to the system keyring")
    p.add_argument("--qr", action="store_true", help="sign in by scanning a QR code")
    p.add_argument("--qr-light", action="store_true", help="QR for a light terminal background")
    p = sub.add_parser("logout", help="end the session on the server and delete credentials")
    p.add_argument("--force", action="store_true", help="delete credentials even if the server is unreachable")
    sub.add_parser("status", help="show whether you are logged in and where credentials are stored")
    p = sub.add_parser("init", help="write a starter config that protects all existing folders")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    sub.add_parser("show", help="list folders and all groups/channels with their ids (read-only)")
    p = sub.add_parser("plan", help="show what apply would change (read-only)")
    p.add_argument("-q", "--quiet", action="store_true", help="do not list unmatched chats")
    p = sub.add_parser("apply", help="apply the plan after confirmation")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-q", "--quiet", action="store_true", help="do not list unmatched chats")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = config_path(args.config)
    try:
        if args.cmd == "status":
            store = default_store()
            print(("logged in" if store.load() else "not logged in") + f"; store: {store.where}")
            return 0
        if args.cmd == "login":
            name = asyncio.run(auth.login(qr=args.qr, invert=not args.qr_light))
            print(f"logged in as {name}")
            return 0
        if args.cmd == "logout":
            print(asyncio.run(auth.logout(force=args.force)))
            return 0
        if args.cmd == "init":
            return asyncio.run(_init(path, args.force))
        if args.cmd == "show":
            asyncio.run(_show(path))
            return 0
        return asyncio.run(_plan(path, apply=args.cmd == "apply", yes=getattr(args, "yes", False),
                                 quiet=args.quiet))
    except (folders.ConfigError, folders.PlanError, StoreError, auth.TelegramError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("interrupted", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — Telethon/network
        from telethon.errors import RPCError
        if isinstance(e, RPCError):          # API errors carry a code, not user input
            print(f"Telegram error: {e}", file=sys.stderr)
            return 1
        # no message text for anything else: it may echo what was typed at a prompt
        print(f"error: {e.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
