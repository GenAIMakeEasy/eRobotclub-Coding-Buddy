"""Student levels: the admin CRUD, the public dropdown list (names only),
and the level's prompt reaching the model's system prompt."""

import json
import time

import pytest

from fastapi.testclient import TestClient

from app.admin.auth import AdminAuth
from app.api.main import create_app
from app.llm.ollama_client import ChatResult
from app.queue.manager import QueueManager
from app.session.log import LogWriter
from app.session.store import FileSessionStore
from app.site_auth import SiteAuth


class RecordingClient:
    model = "fake"

    def __init__(self):
        self.systems = []

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.systems.append(system)
        body = {"kind": "complete", "voice": "x", "message": "x", "blocks": []}
        return ChatResult(text=json.dumps(body), model=self.model)


class NoEmbedder:
    async def embed(self, text):
        return [1.0, 0.0]


@pytest.fixture
def env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_BACKEND=local\n", encoding="utf-8")
    client = RecordingClient()
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=1, max_queue_depth=5, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        admin_auth=AdminAuth(password="pw"),
        site_auth=SiteAuth(password=None),
        env_path=env_path,
        skills_path=tmp_path / "skills.json",
        prompts_path=tmp_path / "prompts.json",
        levels_path=tmp_path / "levels.json",
        directory_path=tmp_path / "directory.json",
        embedder=NoEmbedder(),
        restart_fn=lambda: None,
    )
    # Context manager: the queue's workers only start in the app lifespan.
    with TestClient(app) as http:
        token = http.post("/admin/login", json={"password": "pw"}).json()["token"]
        yield http, client, {"Authorization": f"Bearer {token}"}


def test_admin_crud_and_public_list_hides_prompts(env):
    http, _, auth = env
    assert http.get("/levels").json() == []
    assert http.put("/admin/levels", json=[]).status_code == 401

    saved = http.put(
        "/admin/levels",
        headers=auth,
        json=[{"name": "Beginner", "prompt": "Keep it tiny {x}"}, {"name": "Advanced", "prompt": ""}],
    ).json()
    assert [lv["name"] for lv in saved] == ["Beginner", "Advanced"]
    assert all(lv["id"] for lv in saved)

    public = http.get("/levels").json()
    assert public == [{"id": lv["id"], "name": lv["name"]} for lv in saved]

    # set (edit) one and remove the other
    saved[0]["prompt"] = "Edited"
    again = http.put("/admin/levels", headers=auth, json=[saved[0]]).json()
    assert again == [saved[0]]
    assert http.get("/admin/levels", headers=auth).json() == again


def test_duplicate_or_blank_names_rejected(env):
    http, _, auth = env
    dup = [{"name": "A", "prompt": ""}, {"name": "a", "prompt": ""}]
    assert http.put("/admin/levels", headers=auth, json=dup).status_code == 400
    assert http.put("/admin/levels", headers=auth, json=[{"name": "  ", "prompt": ""}]).status_code == 400


def test_level_prompt_reaches_the_model(env):
    http, client, auth = env
    lv = http.put("/admin/levels", headers=auth, json=[{"name": "Beginner", "prompt": "BEGINNER-RULES {braces}"}]).json()[0]

    def ask(level):
        job = http.post(
            "/ask", json={"device_id": "d", "mode": "build", "question": "move forward", "level": level}
        ).json()["job_id"]
        for _ in range(100):
            if http.get(f"/answer/{job}").json().get("status") in ("done", "failed"):
                break
            time.sleep(0.05)

    ask(lv["id"])
    ask("unknown-id")
    ask(None)
    assert "BEGINNER-RULES {braces}" in client.systems[0]
    assert "BEGINNER-RULES" not in client.systems[1]
    assert "BEGINNER-RULES" not in client.systems[2]


def test_directory_entries_are_editable_and_a_level_picks_its_keys(env):
    http, client, auth = env
    default = http.get("/admin/directory-context", headers=auth).json()
    assert {"Control", "Motion"} <= {e["key"] for e in default}  # seeded from the vendored directory

    entries = [{"key": "Basics", "value": "BASICS-TEXT"}, {"key": "Extra", "value": "EXTRA-TEXT"}]
    assert http.put("/admin/directory-context", headers=auth, json=entries).json() == entries
    dup = http.put("/admin/directory-context", headers=auth, json=entries + [entries[0]])
    assert dup.status_code == 400

    lv = http.put(
        "/admin/levels",
        headers=auth,
        json=[
            {"name": "Beginner", "prompt": "", "directory_keys": ["Basics", "nope"]},
            {"name": "Plain", "prompt": "", "directory_keys": []},
        ],
    ).json()
    assert lv[0]["directory_keys"] == ["Basics"]  # unknown key dropped

    def ask(level):
        job = http.post("/ask", json={"device_id": "d", "mode": "build", "question": "move forward", "level": level}).json()["job_id"]
        for _ in range(100):
            if http.get(f"/answer/{job}").json().get("status") in ("done", "failed"):
                break
            time.sleep(0.05)

    ask(lv[0]["id"])
    ask(lv[1]["id"])
    assert "BASICS-TEXT" in client.systems[0] and "EXTRA-TEXT" not in client.systems[0]
    assert "BASICS-TEXT" not in client.systems[1]  # no keys ticked -> normal routing

    # removing a key unticks it on every level
    http.put("/admin/directory-context", headers=auth, json=[entries[1]])
    assert http.get("/admin/levels", headers=auth).json()[0]["directory_keys"] == []


def test_default_recover_and_factory_reset(env, tmp_path, monkeypatch):
    from app.schema import block_directory as bd

    # Never touch the real vendored files: point the module at tmp copies.
    md, factory, sidecar = tmp_path / "dir.md", tmp_path / "dir.factory.md", tmp_path / "dir.sha256"
    md.write_bytes(bd.FACTORY_PATH.read_bytes())
    factory.write_bytes(bd.FACTORY_PATH.read_bytes())
    monkeypatch.setattr(bd, "DIRECTORY_PATH", md)
    monkeypatch.setattr(bd, "FACTORY_PATH", factory)
    monkeypatch.setattr(bd, "SHA_SIDECAR_PATH", sidecar)
    http, _, auth = env

    entries = http.get("/admin/directory-context", headers=auth).json()
    for e in entries:
        if e["key"] == "Sound":
            e["value"] += "\nEXTRA SOUND NOTE"
    http.put("/admin/directory-context", headers=auth, json=entries)

    # normal save did not touch the default copy
    assert "EXTRA SOUND NOTE" not in md.read_text()

    assert http.post("/admin/directory-context/set-default", headers=auth).status_code == 200
    assert "EXTRA SOUND NOTE" in md.read_text() and sidecar.exists()
    assert {b.name for b in bd.parse_text(md.read_text())} == {b.name for b in bd.parse_text(factory.read_text())}

    # edit the working copy, then recover from the default copy
    http.put("/admin/directory-context", headers=auth, json=[{"key": "Only", "value": "x"}])
    rec = http.post("/admin/directory-context/recover", headers=auth).json()
    assert any("EXTRA SOUND NOTE" in e["value"] for e in rec["entries"])

    # factory reset restores the original file, drops the sidecar, resets entries
    fr = http.post("/admin/directory-context/factory-reset", headers=auth).json()
    assert md.read_bytes() == factory.read_bytes() and not sidecar.exists()
    assert not any("EXTRA SOUND NOTE" in e["value"] for e in fr["entries"])

    # a default that would leave no blocks is refused, file untouched
    http.put("/admin/directory-context", headers=auth, json=[{"key": "Empty", "value": "nothing"}])
    before = md.read_bytes()
    assert http.post("/admin/directory-context/set-default", headers=auth).status_code == 400
    assert md.read_bytes() == before


def test_a_level_cannot_use_blocks_from_tabs_it_did_not_pick():
    from app.llm.pipeline import _restrict_block_names
    from app.schema import block_directory as bd
    from app.schema.answer_contract import schema_for

    allowed = [b.name for b in bd.load() if b.tab in ("Control", "Motion")]
    function_blocks = {b.name for b in bd.load() if b.tab == "Function"}
    assert function_blocks
    full, narrowed = schema_for("build"), _restrict_block_names(schema_for("build"), allowed)

    def enums(node, out):
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list) and "Forever" in node["enum"]:
                out.append(node["enum"])
            [enums(v, out) for v in node.values()]
        elif isinstance(node, list):
            [enums(v, out) for v in node]
        return out

    assert function_blocks & set(enums(full, [])[0])
    only = enums(narrowed, [])[0]
    assert set(only) == set(allowed) and not function_blocks & set(only)
    assert function_blocks & set(enums(schema_for("build"), [])[0])  # original untouched


def test_level_with_directory_keys_narrows_the_schema_sent_to_the_model(env):
    http, client, auth = env
    http.put("/admin/directory-context", headers=auth, json=[{"key": "Control", "value": "CTRL"}])
    lv = http.put("/admin/levels", headers=auth, json=[{"name": "Spark", "prompt": "", "directory_keys": ["Control"]}]).json()[0]
    seen = {}
    orig = client.chat

    async def spy(system, user, schema, **kw):
        seen["schema"] = schema
        seen["system"] = system
        return await orig(system, user, schema, **kw)

    client.chat = spy
    job = http.post("/ask", json={"device_id": "d", "mode": "build", "question": "x", "level": lv["id"]}).json()["job_id"]
    for _ in range(100):
        if http.get(f"/answer/{job}").json().get("status") in ("done", "failed"):
            break
        time.sleep(0.05)
    text = json.dumps(seen["schema"])
    assert '"Forever"' in text and '"Create a function"' not in text
    assert "ONLY the blocks listed" in seen["system"]


def test_a_slow_attempt_is_abandoned_and_retried(monkeypatch):
    import asyncio

    from app import config
    from app.llm.pipeline import answer

    monkeypatch.setattr(config, "ASK_ATTEMPT_TIMEOUT_SECONDS", 0.05)

    class SlowThenFast:
        calls = 0

        async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
            SlowThenFast.calls += 1
            if SlowThenFast.calls == 1:
                await asyncio.sleep(5)
            body = {"kind": "ask", "voice": "x", "message": "ok", "blocks": [], "note": None, "image_note": None}
            return ChatResult(text=json.dumps(body), model="m")

    result = asyncio.run(answer(SlowThenFast(), "build", "hi"))
    assert SlowThenFast.calls == 2 and result.answer is not None

    class AlwaysSlow:
        async def chat(self, *a, **k):
            await asyncio.sleep(5)

    from app.llm.ollama_client import ModelUnavailable

    with pytest.raises(ModelUnavailable, match="on the retry"):
        asyncio.run(answer(AlwaysSlow(), "build", "hi"))


def test_no_variable_note_only_when_variable_blocks_are_not_allowed():
    from app.llm.pipeline import _level_blocks_note
    from app.schema import block_directory as bd

    without = [b.name for b in bd.load() if b.tab in ("Control", "Sensing", "Motion")]
    with_vars = without + [b.name for b in bd.load() if b.tab == "Variable"]
    assert "100 for an IR sensor threshold" in _level_blocks_note(without)
    assert "100 for an IR sensor threshold" not in _level_blocks_note(with_vars)


def test_variable_and_function_notes_follow_the_levels_tabs():
    from app.llm.pipeline import _level_blocks_note
    from app.schema import block_directory as bd

    def names(*tabs):
        return [b.name for b in bd.load() if b.tab in tabs]

    spark = _level_blocks_note(names("Control", "Sensing", "Motion"))
    assert "Variables are not available" in spark and "ARE available" not in spark
    excel = _level_blocks_note(names("Control", "Sensing", "Motion", "Variable", "Function"))
    assert "Variables ARE available" in excel and "Functions ARE available" in excel
    assert "not available" not in excel
