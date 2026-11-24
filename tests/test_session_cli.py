"""Credential stores and the CLI. No network, no real keyring."""

import json
import stat
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from telethon.tl.functions.help import GetAppConfigRequest
from telethon.tl.types import DialogFilter, TextWithEntities
from test_folders import FakeClient

from tgfolders import auth, cli, folders, session

CREDS = {"api_id": 1, "api_hash": "h", "session": "s"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TGFOLDERS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TGFOLDERS_SESSION_FILE", str(tmp_path / "cfg" / "session.json"))


def test_file_store_roundtrip_and_mode(tmp_path):
    st = session.FileStore(tmp_path / "x" / "session.json")
    assert st.load() is None
    st.save(CREDS)
    assert st.load() == CREDS
    assert stat.S_IMODE(st.path.stat().st_mode) == 0o600
    st.delete()
    assert st.load() is None
    st.path.write_text("{")
    with pytest.raises(session.StoreError):
        st.load()


class MemKeyring:
    priority = 5

    def __init__(self):
        self.d = {}

    def get_password(self, s, k):
        return self.d.get((s, k))

    def set_password(self, s, k, v):
        self.d[(s, k)] = v

    def delete_password(self, s, k):
        self.d.pop((s, k), None)


def test_keyring_store_roundtrip():
    st = session.KeyringStore(MemKeyring())
    st.save(CREDS)
    assert st.load() == CREDS
    st.delete()
    assert st.load() is None


def test_default_store_env_file(tmp_path):
    assert isinstance(session.default_store(), session.FileStore)


def test_default_store_falls_back_to_file_without_keyring(monkeypatch):
    monkeypatch.delenv("TGFOLDERS_SESSION_FILE")
    import keyring
    from keyring.backends import fail
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert isinstance(session.default_store(), session.FileStore)
    monkeypatch.setattr(keyring, "get_keyring", lambda: MemKeyring())
    assert isinstance(session.default_store(), session.KeyringStore)


def test_status_and_not_logged_in(capsys):
    assert cli.main(["status"]) == 0
    assert "not logged in" in capsys.readouterr().out
    assert cli.main(["show"]) == 1
    assert "tgfolders login" in capsys.readouterr().err


def test_logout_when_not_logged_in(capsys):
    assert cli.main(["logout"]) == 0
    assert "not logged in" in capsys.readouterr().out


class CliClient(FakeClient):
    async def __call__(self, req):
        if isinstance(req, GetAppConfigRequest):
            return SimpleNamespace(config=None)          # -> fallback limits
        return await super().__call__(req)

    async def get_me(self):
        return SimpleNamespace(premium=False)


def _fake_connected(monkeypatch, filters):
    client = CliClient(filters)

    @asynccontextmanager
    async def connected(store=None):
        yield client

    monkeypatch.setattr(auth, "connected", connected)
    return client


def test_init_then_plan_then_apply(monkeypatch, tmp_path, capsys):
    fam = DialogFilter(id=2, title=TextWithEntities(text="Family", entities=[]), pinned_peers=[],
                       include_peers=[], exclude_peers=[])
    client = _fake_connected(monkeypatch, [fam])
    cfg_path = tmp_path / "cfg" / "config.json"

    assert cli.main(["init"]) == 0
    raw = json.loads(cfg_path.read_text())
    assert raw["protected"] == ["Family"]
    assert cli.main(["init"]) == 1                       # no silent overwrite

    raw["folders"] = [{"title": "Empty", "keywords": ["nothing"]}]
    cfg_path.write_text(json.dumps(raw))
    capsys.readouterr()
    assert cli.main(["plan"]) == 0
    assert "[Empty] (new): +0" in capsys.readouterr().out
    assert cli.main(["apply", "--yes"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert client.writes == []

    raw["folders"] = [{"title": "Family", "keywords": ["x"]}]
    cfg_path.write_text(json.dumps(raw))
    assert cli.main(["apply", "--yes"]) == 1
    assert "protected" in capsys.readouterr().err
    assert client.writes == []


def test_describe_sent():
    sent = type("S", (), {"type": type("SentCodeTypeApp", (), {})(), "next_type": None, "timeout": None})()
    assert "Telegram app" in auth.describe_sent(sent)


def test_folders_module_has_no_default_protection():
    assert folders.parse_config({"folders": []}).protected == []


def test_login_refuses_without_terminal(monkeypatch, capsys):
    monkeypatch.setattr(auth, "interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt"))
    assert cli.main(["login"]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_unattended_apply_prints_notice(monkeypatch, tmp_path, capsys):
    from test_folders import ch, folder
    monkeypatch.setattr(auth, "interactive", lambda: False)
    py = ch(1, "Python club")
    client = _fake_connected(monkeypatch, [folder(2, "Family")])
    client.dialogs = [SimpleNamespace(id=py.id, name=py.title, is_user=False, is_group=True,
                                      is_channel=True, input_entity=py.peer, folder_id=None)]
    cfg_path = tmp_path / "cfg" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"protected": ["Family"],
                                    "folders": [{"title": "Code", "keywords": ["python"]}]}))
    assert cli.main(["apply", "--yes"]) == 0
    assert "not reviewed by a person" in capsys.readouterr().err
    assert len(client.writes) == 1
