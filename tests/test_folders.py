"""Folder logic. No network: the Telethon client is a fake, TL types are real."""

import asyncio
from types import SimpleNamespace as NS

import pytest
from telethon.tl.functions.chatlists import LeaveChatlistRequest
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.types import (DialogFilter, DialogFilterChatlist, DialogFilterDefault,
                               InputPeerChannel, InputPeerChat, TextWithEntities)
from telethon.tl.types.messages import DialogFilters

from tgfolders import folders as tf


def ch(cid, title, kind="group"):
    return tf.Chat(id=-1000000000000 - cid, title=title, kind=kind, peer=InputPeerChannel(cid, 0))


def folder(fid, title, chats=(), **kw):
    return DialogFilter(id=fid, title=TextWithEntities(text=title, entities=[]), pinned_peers=[],
                        include_peers=[c.peer for c in chats], exclude_peers=[], **kw)


def shared(fid, title, chats=()):
    return DialogFilterChatlist(id=fid, title=TextWithEntities(text=title, entities=[]),
                                pinned_peers=[], include_peers=[c.peer for c in chats])


def cfg(folders, **kw):
    return tf.parse_config({"protected": ["Family", "Work"], "folders": folders, **kw})


PY = ch(1, "Python club")
COURSE = ch(2, "Course: data structures")
LOCAL = ch(3, "Neighbours chat")
NEWS = ch(4, "Daily news", kind="channel")
OTHER = ch(5, "Random chat")


# --- matching and config ------------------------------------------------------------

def test_matches_word_prefix():
    assert tf.matches("AI-Magic", "ai") and tf.matches("News | AI", "ai")
    assert not tf.matches("Daily news", "ai")
    assert tf.matches("learn.java", "java")
    assert tf.matches("Книги для программистов", "книг") and not tf.matches("Аудиокниги", "книг")


def test_protected_comes_only_from_config_and_cannot_be_targeted():
    c = cfg([{"title": "Study", "keywords": ["course"]}])
    assert c.is_protected("🏠 family") and c.is_protected("WORK")
    assert not tf.parse_config({"folders": []}).is_protected("Work")
    for t in ("Work", "family"):
        with pytest.raises(tf.ConfigError):
            cfg([{"title": t, "keywords": ["x"]}])
    with pytest.raises(tf.ConfigError):
        cfg([{"title": "A", "keywords": ["x"]}], delete=["Family"])


@pytest.mark.parametrize("bad", [
    {"folders": [{"title": "A name that is too long"}]},
    {"folders": [{"title": "A"}, {"title": "a"}]},
    {"folders": [{"title": "A", "chats": ["1"]}]},
    {"folders": [{"title": "A", "keywords": [""]}]},
    {"folders": [{"title": "A"}], "delete": ["A"]},
    {"folders": [], "premium": True},
    {"folders": "nope"},
])
def test_config_validation(bad):
    with pytest.raises(tf.ConfigError):
        tf.parse_config(bad)


def test_underscore_keys_are_comments():
    assert tf.parse_config({"_help": "x", "folders": []}).folders == []


def test_load_config(tmp_path):
    with pytest.raises(tf.ConfigError, match="tgfolders init"):
        tf.load_config(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(tf.ConfigError, match="invalid JSON"):
        tf.load_config(tmp_path / "bad.json")


# --- plan ---------------------------------------------------------------------------

def test_plan_first_match_wins_and_new_ids_skip_taken():
    filters = [folder(2, "Family"), folder(3, "Work"), folder(5, "Local", [LOCAL])]
    plan = tf.build_plan(filters, [PY, COURSE, LOCAL, NEWS, OTHER],
                         cfg([{"title": "Study", "keywords": ["course", "python"]},
                              {"title": "News", "keywords": ["news", "course"]}]))
    assert not plan.errors
    by = {c.rule.title: c for c in plan.changes}
    assert [x.title for x in by["Study"].add] == ["Python club", "Course: data structures"]
    assert [x.title for x in by["News"].add] == ["Daily news"]
    assert by["Study"].folder_id == 4 and by["News"].folder_id == 6
    assert plan.skipped_sorted == [LOCAL]
    assert plan.unmatched == [OTHER]


def test_sorted_chat_moves_only_by_explicit_id_or_include_sorted():
    filters = [folder(2, "Local", [LOCAL])]
    rules = [{"title": "Home", "keywords": ["neighbours"]}]
    assert not tf.build_plan(filters, [LOCAL], cfg(rules)).writes
    assert tf.build_plan(filters, [LOCAL], cfg(rules, include_sorted=True)).writes[0].add == [LOCAL]
    p = tf.build_plan(filters, [LOCAL], cfg([{"title": "Home", "chats": [LOCAL.id]}]))
    assert p.writes[0].add == [LOCAL]


def test_existing_folder_is_add_only_and_keeps_its_fields():
    old = folder(4, "Study", [COURSE], groups=True, emoticon="📚")
    plan = tf.build_plan([old], [PY, COURSE], cfg([{"title": "study", "keywords": ["python", "course"]}]))
    c = plan.writes[0]
    assert c.add == [PY] and c.folder_id == 4
    f = tf.make_filter(c)
    assert [tf.peer_id(p) for p in f.include_peers] == [COURSE.id, PY.id]
    assert f.groups and f.emoticon == "📚" and tf.title_of(f) == "Study"
    assert len(old.include_peers) == 1          # the original is not mutated


def test_new_folder_filter():
    plan = tf.build_plan([], [PY], cfg([{"title": "Code", "emoticon": "💻", "keywords": ["python"]}]))
    f = tf.make_filter(plan.writes[0])
    assert f.id == 2 and tf.title_of(f) == "Code" and f.emoticon == "💻"
    assert f.exclude_peers == [] and f.pinned_peers == []


def test_limits_shared_and_unknown_chat():
    filters = [folder(i, f"F{i}") for i in range(2, 12)]
    p = tf.build_plan(filters, [PY], cfg([{"title": "New", "keywords": ["python"]}]))
    assert any("limit is 10" in e for e in p.errors)
    p = tf.build_plan(filters, [PY], cfg([{"title": "New", "keywords": ["python"]}]), tf.Limits(30, 200))
    assert not p.errors

    many = [ch(100 + i, f"g{i}") for i in range(101)]
    p = tf.build_plan([], many, cfg([{"title": "All", "keywords": ["g"]}]))
    assert any("limit is 100" in e for e in p.errors)

    p = tf.build_plan([shared(7, "Shared")], [PY], cfg([{"title": "Shared", "keywords": ["python"]}]))
    assert any("shared folder" in e for e in p.errors)

    p = tf.build_plan([], [PY], cfg([{"title": "X", "chats": [123]}]))
    assert any("not found" in e for e in p.errors)


def test_delete_frees_slots_and_unsorts_its_chats():
    filters = [folder(i, f"F{i}") for i in range(5, 13)] + [shared(3, "Picks", [PY]), folder(4, "Old", [COURSE])]
    c = cfg([{"title": "Code", "keywords": ["python"]}, {"title": "Courses", "keywords": ["course"]}],
            delete=["picks", "Old"])
    plan = tf.build_plan(filters, [PY, COURSE], c)
    assert not plan.errors
    assert [f.id for f in plan.deletes] == [3, 4]
    by = {x.rule.title: x for x in plan.changes}
    assert by["Code"].add == [PY] and by["Courses"].add == [COURSE]
    assert {by["Code"].folder_id, by["Courses"].folder_id}.isdisjoint({3, 4})

    p = tf.build_plan([], [], cfg([{"title": "A", "keywords": ["x"]}], delete=["Nope"]))
    assert any("no such folder" in e for e in p.errors)


def test_format_plan_and_show():
    filters = [folder(2, "Family"), folder(4, "Study", [COURSE])]
    c = cfg([{"title": "Study", "keywords": ["python"]}])
    plan = tf.build_plan(filters, [PY, COURSE, OTHER], c)
    text = tf.format_plan(plan)
    assert "[Study] (existing, id 4): +1" in text and "Random chat" in text
    assert "Random chat" not in tf.format_plan(plan, verbose_unmatched=False)
    show = tf.format_show(filters, [PY, COURSE], c, tf.Limits(10, 100))
    assert "Family [protected]" in show and "→ Study" in show and "Folders (2 of 10)" in show


def test_init_config_protects_everything():
    raw = tf.init_config([folder(2, "Family"), shared(3, "Picks")])
    c = tf.parse_config(raw)
    assert c.protected == ["Family", "Picks"] and c.folders == []


def test_limits_from_app_config():
    def kv(k, v):
        return NS(key=k, value=NS(value=v))
    conf = NS(value=[kv("dialog_filters_limit_default", 10.0), kv("dialog_filters_limit_premium", 30.0),
                     kv("dialog_filters_chats_limit_default", 100.0),
                     kv("dialog_filters_chats_limit_premium", 200.0), kv("other", "x")])
    assert tf.limits_from_app_config(conf, premium=False) == tf.Limits(10, 100)
    assert tf.limits_from_app_config(conf, premium=True) == tf.Limits(30, 200)
    assert tf.limits_from_app_config(None, premium=True) == tf.Limits()


# --- client I/O ---------------------------------------------------------------------

class FakeClient:
    def __init__(self, filters, dialogs=()):
        self.filters = filters
        self.dialogs = list(dialogs)
        self.writes = []

    async def __call__(self, req):
        if isinstance(req, GetDialogFiltersRequest):
            return DialogFilters(filters=[DialogFilterDefault(), *self.filters])
        if isinstance(req, (UpdateDialogFilterRequest, LeaveChatlistRequest)):
            self.writes.append(req)
            return True
        raise AssertionError(f"unexpected request {req!r}")

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d


def test_read_state_keeps_groups_and_channels_only():
    dialogs = [NS(id=PY.id, name=PY.title, is_user=False, is_group=True, is_channel=True,
                  input_entity=PY.peer, folder_id=None),
               NS(id=7, name="A person", is_user=True, is_group=False, is_channel=False, input_entity=None),
               NS(id=-50, name="Old group", is_user=False, is_group=True, is_channel=False,
                  input_entity=InputPeerChat(50), folder_id=1),
               NS(id=-1009, name="A channel", is_user=False, is_group=False, is_channel=True,
                  input_entity=InputPeerChannel(9, 0), folder_id=None)]
    filters, chats = asyncio.run(tf.read_state(FakeClient([folder(2, "Work")], dialogs)))
    assert [tf.title_of(f) for f in filters] == ["Work"]
    assert [(c.title, c.kind, c.archived) for c in chats] == [
        ("Python club", "group", False), ("Old group", "group", True), ("A channel", "channel", False)]


def test_write_deletes_first_and_never_touches_protected():
    filters = [folder(2, "Work"), shared(3, "Picks", [PY]), folder(4, "Old")]
    c = cfg([{"title": "Code", "keywords": ["python"]}], delete=["Picks", "Old"])
    plan = tf.build_plan(filters, [PY], c)
    client = FakeClient(filters)
    assert asyncio.run(tf.write(client, plan, c)) == 3
    kinds = [type(r).__name__ for r in client.writes]
    assert kinds == ["LeaveChatlistRequest", "UpdateDialogFilterRequest", "UpdateDialogFilterRequest"]
    assert client.writes[0].peers == []              # chats of the shared folder are kept
    assert client.writes[1].filter is None           # plain delete
    assert client.writes[2].id == 5 and client.writes[2].filter is not None
    assert all(getattr(r, "id", None) != 2 for r in client.writes)


def test_write_rechecks_server_state():
    work = folder(2, "Work")
    c = cfg([{"title": "Code", "keywords": ["python"]}])
    plan = tf.build_plan([work], [PY], c)
    plan.writes[0].folder_id = 2                     # now points at a protected folder
    with pytest.raises(tf.PlanError):
        asyncio.run(tf.write(FakeClient([work]), plan, c))

    plan = tf.build_plan([work], [PY], c)            # new id 3 got taken meanwhile
    with pytest.raises(tf.PlanError, match="taken"):
        asyncio.run(tf.write(FakeClient([work, folder(3, "Someone")]), plan, c))

    old = folder(4, "Old")
    c2 = cfg([{"title": "Code", "keywords": ["python"]}], delete=["Old"])
    plan = tf.build_plan([old], [PY], c2)            # the folder was renamed meanwhile
    with pytest.raises(tf.PlanError, match="changed"):
        asyncio.run(tf.write(FakeClient([folder(4, "Renamed")]), plan, c2))

    with pytest.raises(tf.PlanError):
        asyncio.run(tf.write(FakeClient([]), tf.Plan([], [], [], [], ["x"]), c))
