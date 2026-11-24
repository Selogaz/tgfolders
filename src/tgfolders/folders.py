"""Folder logic: config, plan, write. Pure functions + a thin async layer over a Telethon client.

Guarantees:
- protected folders (``protected`` in the config) are never modified or deleted: such a plan is
  rejected, and the write step re-reads folders from the server and checks ids again;
- shared folders (joined via an invite link) are never modified; they can only be removed as a
  whole, without leaving any of their chats;
- only folders listed in ``delete`` are deleted; chats stay in the account;
- otherwise add-only: nothing is removed from a folder, its flags/emoji/pinned chats are kept;
- account limits (folders per account, explicitly added chats per folder) are read from the
  server for your account type; if the plan exceeds them, it is rejected as a whole and
  nothing is written.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

# Fallbacks; real values come from help.getAppConfig (dialog_filters_*_limit_default/premium).
FOLDERS_MAX = 10       # 30 with Premium
PEERS_MAX = 100        # pinned + explicitly included chats per folder; 200 with Premium
TITLE_MAX = 12         # Telegram limit on a folder name
ARCHIVE_ID = 1         # id 0 is "All chats", 1 is the archive; user folders use 2..255
FIRST_ID, LAST_ID = 2, 255


class ConfigError(ValueError):
    """Config is missing or invalid."""


class PlanError(RuntimeError):
    """The plan violates protection or limits; nothing is written."""


def norm(title: str) -> str:
    """Title comparison: case-, emoji- and whitespace-insensitive ("🏠 Home" == "home")."""
    return "".join(ch for ch in title.casefold() if ch.isalnum())


def title_of(f) -> str:
    t = getattr(f, "title", "")
    return getattr(t, "text", t) or ""


def matches(title: str, keyword: str) -> bool:
    """The keyword matches the beginning of any word in the title, case-insensitively.

    "ai" matches "AI-Magic" and "News | AI", but not "Daily"."""
    t, k = title.casefold(), keyword.casefold()
    i = t.find(k)
    while i != -1:
        if i == 0 or not t[i - 1].isalnum():
            return True
        i = t.find(k, i + 1)
    return False


# --- config -------------------------------------------------------------------------

@dataclass
class FolderRule:
    title: str
    keywords: list[str] = field(default_factory=list)
    chats: list[int] = field(default_factory=list)
    emoticon: str | None = None


@dataclass
class Config:
    protected: list[str]
    folders: list[FolderRule]
    include_sorted: bool = False
    delete: list[str] = field(default_factory=list)

    def is_protected(self, title: str) -> bool:
        n = norm(title)
        return any(n == norm(p) for p in self.protected)


@dataclass(frozen=True)
class Limits:
    folders: int = FOLDERS_MAX
    peers: int = PEERS_MAX


def _str_list(raw: dict, key: str) -> list[str]:
    v = raw.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ConfigError(f"{key}: expected a list of non-empty strings")
    return [x.strip() for x in v]


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the config root must be an object")
    known = {"protected", "folders", "include_sorted", "delete"}
    unknown = sorted(k for k in raw if k not in known and not k.startswith("_"))
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    folders_raw = raw.get("folders", [])
    if not isinstance(folders_raw, list):
        raise ConfigError("folders: expected a list")
    cfg = Config(protected=_str_list(raw, "protected"), folders=[],
                 include_sorted=bool(raw.get("include_sorted", False)),
                 delete=_str_list(raw, "delete"))
    for d in cfg.delete:
        if cfg.is_protected(d):
            raise ConfigError(f"'{d}' is protected and cannot be deleted")
    seen: set[str] = set()
    for i, fr in enumerate(folders_raw):
        if not isinstance(fr, dict) or not isinstance(fr.get("title"), str) or not fr["title"].strip():
            raise ConfigError(f"folders[{i}]: 'title' (string) is required")
        title = fr["title"].strip()
        if len(title) > TITLE_MAX:
            raise ConfigError(f"'{title}': Telegram limits folder names to {TITLE_MAX} characters")
        if cfg.is_protected(title):
            raise ConfigError(f"'{title}' is protected; protected folders cannot be targets")
        if norm(title) in seen:
            raise ConfigError(f"'{title}' is listed twice")
        if norm(title) in map(norm, cfg.delete):
            raise ConfigError(f"'{title}' is both in folders and in delete")
        seen.add(norm(title))
        kw = _str_list(fr, "keywords")
        chats = fr.get("chats", [])
        if not isinstance(chats, list) or not all(isinstance(c, int) and not isinstance(c, bool) for c in chats):
            raise ConfigError(f"'{title}': chats must be a list of integer ids")
        emo = fr.get("emoticon")
        if emo is not None and not isinstance(emo, str):
            raise ConfigError(f"'{title}': emoticon must be a string")
        cfg.folders.append(FolderRule(title, kw, list(chats), emo))
    return cfg


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}; create one with `tgfolders init`") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON: {e}") from None
    return parse_config(raw)


# --- account state ------------------------------------------------------------------

@dataclass
class Chat:
    id: int            # Telethon "marked" id (-100… for channels and supergroups)
    title: str
    kind: str          # group | channel
    peer: object       # InputPeer
    archived: bool = False


def peer_id(p) -> int:
    from telethon import utils
    return utils.get_peer_id(p)


def user_filters(resp) -> list:
    """User folders (without "All chats"). messages.DialogFilters or, on old layers, a list."""
    from telethon.tl.types import DialogFilterDefault
    items = getattr(resp, "filters", resp)
    return [f for f in items if not isinstance(f, DialogFilterDefault)]


def is_shared(f) -> bool:
    from telethon.tl.types import DialogFilterChatlist
    return isinstance(f, DialogFilterChatlist)


def explicit_ids(f) -> set[int]:
    return {peer_id(p) for p in (*getattr(f, "pinned_peers", []), *getattr(f, "include_peers", []))}


def chat_from_dialog(d) -> Chat | None:
    if getattr(d, "is_user", False):
        return None
    if not (getattr(d, "is_group", False) or getattr(d, "is_channel", False)):
        return None
    kind = "group" if getattr(d, "is_group", False) else "channel"
    return Chat(id=d.id, title=getattr(d, "name", None) or getattr(d, "title", "") or f"id{d.id}",
                kind=kind, peer=d.input_entity, archived=getattr(d, "folder_id", None) == ARCHIVE_ID)


# --- plan ---------------------------------------------------------------------------

@dataclass
class Change:
    rule: FolderRule
    existing: object | None          # DialogFilter, or None for a new folder
    folder_id: int | None
    add: list[Chat] = field(default_factory=list)


@dataclass
class Plan:
    changes: list[Change]
    deletes: list                    # DialogFilter / DialogFilterChatlist
    unmatched: list[Chat]
    skipped_sorted: list[Chat]
    errors: list[str]

    @property
    def writes(self) -> list[Change]:
        return [c for c in self.changes if c.add]

    @property
    def n_ops(self) -> int:
        return len(self.deletes) + len(self.writes)


def build_plan(filters: list, chats: list[Chat], cfg: Config, limits: Limits = Limits()) -> Plan:
    errors: list[str] = []
    all_titles = {norm(title_of(f)): f for f in filters}
    deletes = []
    for d in cfg.delete:
        f = all_titles.get(norm(d))
        if f is None:
            errors.append(f"delete '{d}': no such folder")
        elif cfg.is_protected(title_of(f)):              # belt and braces: parse_config forbids it
            errors.append(f"'{d}' is protected")
        else:
            deletes.append(f)
    del_ids = {f.id for f in deletes}
    used_ids = {f.id for f in filters}                   # ids of deleted folders are not reused
    filters = [f for f in filters if f.id not in del_ids]
    by_title = {norm(title_of(f)): f for f in filters}
    sorted_ids: set[int] = set()
    for f in filters:
        sorted_ids |= explicit_ids(f)

    changes: list[Change] = []
    for rule in cfg.folders:
        f = by_title.get(norm(rule.title))
        if f is not None and is_shared(f):
            errors.append(f"'{rule.title}' is a shared folder (invite link); not touching it")
        if f is not None and cfg.is_protected(title_of(f)):
            errors.append(f"'{rule.title}' matches a protected folder")
        changes.append(Change(rule, f, f.id if f is not None else None))

    by_chat_id = {c.id: c for c in chats}
    taken: set[int] = set()
    # 1) explicit ids win over keywords
    for ch in changes:
        for cid in ch.rule.chats:
            c = by_chat_id.get(cid)
            if c is None:
                errors.append(f"'{ch.rule.title}': chat {cid} not found among groups/channels")
                continue
            if cid in taken:
                continue
            taken.add(cid)
            if ch.existing is None or cid not in explicit_ids(ch.existing):
                ch.add.append(c)
    # 2) keywords, first matching folder wins
    unmatched, skipped = [], []
    for c in chats:
        if c.id in taken:
            continue
        if c.id in sorted_ids and not cfg.include_sorted:
            skipped.append(c)
            continue
        ch = next((x for x in changes if any(matches(c.title, k) for k in x.rule.keywords)), None)
        if ch is None:
            if c.id not in sorted_ids:
                unmatched.append(c)
            continue
        taken.add(c.id)
        if ch.existing is None or c.id not in explicit_ids(ch.existing):
            ch.add.append(c)

    # new ids and limits
    new = [c for c in changes if c.existing is None and c.add]
    if len(filters) + len(new) > limits.folders:
        errors.append(f"there would be {len(filters) + len(new)} folders; the limit is {limits.folders}")
    free = (i for i in range(FIRST_ID, LAST_ID + 1) if i not in used_ids)
    for c in new:
        c.folder_id = next(free)
    for c in changes:
        if not c.add:
            continue
        before = len(explicit_ids(c.existing)) if c.existing is not None else 0
        if before + len(c.add) > limits.peers:
            errors.append(f"'{c.rule.title}': there would be {before + len(c.add)} chats; "
                          f"the limit is {limits.peers}")
    return Plan(changes, deletes, unmatched, skipped, errors)


def make_filter(change: Change):
    """DialogFilter to write: a copy of the existing folder with chats added, or a new folder."""
    from telethon.tl.types import DialogFilter, TextWithEntities
    peers = [c.peer for c in change.add]
    if change.existing is not None:
        f = copy.copy(change.existing)
        f.include_peers = list(change.existing.include_peers) + peers
        return f
    return DialogFilter(id=change.folder_id, title=TextWithEntities(text=change.rule.title, entities=[]),
                        pinned_peers=[], include_peers=peers, exclude_peers=[],
                        emoticon=change.rule.emoticon)


def format_plan(plan: Plan, verbose_unmatched: bool = True) -> str:
    lines = [f"delete folder: {title_of(f)} (id {f.id}{', shared' if is_shared(f) else ''}; "
             f"chats stay)" for f in plan.deletes]
    for c in plan.changes:
        state = "new" if c.existing is None else f"existing, id {c.folder_id}"
        lines.append(f"[{c.rule.title}] ({state}): +{len(c.add)}")
        lines += [f"    + {x.title} ({x.kind}, {x.id})" for x in c.add]
    if plan.skipped_sorted:
        lines.append(f"already in a folder, left alone: {len(plan.skipped_sorted)}")
    if plan.unmatched:
        lines.append(f"no rule matched: {len(plan.unmatched)}")
        if verbose_unmatched:
            lines += [f"    · {x.title} ({x.kind}, {x.id})" for x in plan.unmatched]
    if plan.errors:
        lines.append("ERRORS (nothing will be written):")
        lines += [f"    ! {e}" for e in plan.errors]
    return "\n".join(lines)


def format_show(filters: list, chats: list[Chat], cfg: Config, limits: Limits = Limits()) -> str:
    member: dict[int, list[str]] = {}
    lines = [f"Folders ({len(filters)} of {limits.folders}):"]
    for f in filters:
        ids = explicit_ids(f)
        for i in ids:
            member.setdefault(i, []).append(title_of(f))
        mark = " [protected]" if cfg.is_protected(title_of(f)) else (" [shared]" if is_shared(f) else "")
        flags = [n for n in ("contacts", "non_contacts", "groups", "broadcasts", "bots") if getattr(f, n, None)]
        extra = f", rules: {', '.join(flags)}" if flags else ""
        lines.append(f"  {f.id:>3}  {title_of(f)}{mark}: {len(ids)}/{limits.peers} chats{extra}")
    lines.append(f"\nGroups and channels ({len(chats)}):")
    for c in sorted(chats, key=lambda x: (x.kind, x.title.casefold())):
        where = ", ".join(member.get(c.id, [])) or "—"
        arch = " (archived)" if c.archived else ""
        lines.append(f"  {c.id:>15}  {c.kind:<7} {c.title}{arch}  → {where}")
    return "\n".join(lines)


def init_config(filters: list) -> dict:
    """Starter config: every existing folder is protected until you decide otherwise."""
    return {
        "protected": [title_of(f) for f in filters],
        "delete": [],
        "include_sorted": False,
        "folders": [],
        "_help": "Move a folder out of 'protected' to let tgfolders add chats to it. "
                 "Example rule: {\"title\": \"Jobs\", \"emoticon\": \"💼\", "
                 "\"keywords\": [\"job\", \"hiring\"], \"chats\": [-1001234567890]}",
    }


# --- client I/O ---------------------------------------------------------------------

def limits_from_app_config(config, premium: bool) -> Limits:
    """Limits from help.getAppConfig (a JSON object of key/value pairs); fallbacks if absent."""
    vals = {}
    for kv in getattr(config, "value", []) or []:
        v = getattr(kv.value, "value", None)
        if isinstance(v, (int, float)):
            vals[kv.key] = int(v)
    kind = "premium" if premium else "default"
    return Limits(folders=vals.get(f"dialog_filters_limit_{kind}", FOLDERS_MAX),
                  peers=vals.get(f"dialog_filters_chats_limit_{kind}", PEERS_MAX))


async def read_limits(client) -> Limits:
    from telethon.tl.functions.help import GetAppConfigRequest
    me = await client.get_me()
    resp = await client(GetAppConfigRequest(hash=0))
    return limits_from_app_config(getattr(resp, "config", None), bool(getattr(me, "premium", False)))


async def read_state(client) -> tuple[list, list[Chat]]:
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    filters = user_filters(await client(GetDialogFiltersRequest()))
    chats = []
    async for d in client.iter_dialogs():
        c = chat_from_dialog(d)
        if c is not None:
            chats.append(c)
    return filters, chats


async def write(client, plan: Plan, cfg: Config) -> int:
    from telethon.tl.functions.chatlists import LeaveChatlistRequest
    from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
    from telethon.tl.types import InputChatlistDialogFilter
    if plan.errors:
        raise PlanError("the plan has errors")
    # re-read: which ids belong to protected/shared folders on the server right now
    current = user_filters(await client(GetDialogFiltersRequest()))
    protected_ids = {f.id for f in current if cfg.is_protected(title_of(f))}
    shared_ids = {f.id for f in current if is_shared(f)}
    by_id = {f.id: f for f in current}
    n = 0
    for f in plan.deletes:                               # deletions first: they free up the limit
        now = by_id.get(f.id)
        if f.id in protected_ids or now is None or norm(title_of(now)) != norm(title_of(f)):
            raise PlanError(f"folder id {f.id} changed or is protected; stopping, run plan again")
        if f.id in shared_ids:
            # peers=[]: remove the folder without leaving any of its chats
            await client(LeaveChatlistRequest(chatlist=InputChatlistDialogFilter(filter_id=f.id), peers=[]))
        else:
            await client(UpdateDialogFilterRequest(id=f.id))
        n += 1
    for c in plan.writes:
        if c.folder_id in protected_ids | shared_ids:
            raise PlanError(f"id {c.folder_id} belongs to a protected/shared folder; stopping")
        if c.existing is None and any(f.id == c.folder_id for f in current):
            raise PlanError(f"id {c.folder_id} is taken; folders changed, run plan again")
        await client(UpdateDialogFilterRequest(id=c.folder_id, filter=make_filter(c)))
        n += 1
    return n
