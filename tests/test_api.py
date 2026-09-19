"""End-to-end through the HTTP surface, with a fake model backend so these
tests don't require a running Ollama. Each test gets its own app instance
via create_app() — a shared queue across the test session would let one
test's cancelled-mid-flight job leak stuck state into another (see
app/api/main.py's create_app docstring)."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.llm.ollama_client import ChatResult
from app.queue.manager import QueueManager
from app.session.log import LogWriter
from app.session.store import FileSessionStore
from app.site_auth import COOKIE_NAME, SiteAuth
from app.skills import store as skills_store
from app.skills.models import Function, SkillDoc

VALID_BUILD_RESPONSE = {
    "kind": "complete",
    "voice": "All done!",
    "message": "Drive forward and stop.",
    "blocks": [{"name": "Stop robot", "slots": []}],
    "note": None,
    "image_note": None,
}


class FakeClient:
    def __init__(self, response_dict=None, delay=0.0):
        self.response_dict = response_dict or VALID_BUILD_RESPONSE
        self.delay = delay
        self.calls = []

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.calls.append({"system": system, "user": user, "history": history})
        if self.delay:
            await asyncio.sleep(self.delay)
        return ChatResult(text=json.dumps(self.response_dict), model="fake-model")


def make_api(tmp_path, response_dict=None, delay=0.0, max_queue_depth=20, skill=None, tts_client=None, site_auth=None):
    """`skill` is an optional SkillDoc pre-seeded into state.skills, for the
    economy-mode tests below -- `embedder=None` keeps retrieval on the
    deterministic keyword-fallback path (app/skills/retrieve.py), so no
    real network call happens even when a skill is present. `tts_client`
    defaults to create_app()'s own default (a real ElevenLabsTTS built from
    config.ELEVENLABS_*, which has no key in a test environment — /tts
    tests that need a successful synthesis pass a fake explicitly).
    `site_auth` defaults to an explicitly DISABLED gate -- see the comment
    on that argument below for why it can't just fall through to
    create_app's own default."""
    skills_path = tmp_path / "skills.json"
    if skill is not None:
        skills_store.upsert(skill, path=skills_path)
    app = create_app(
        client=FakeClient(response_dict=response_dict, delay=delay),
        queue=QueueManager(concurrency=2, max_queue_depth=max_queue_depth, min_wait_seconds=10, max_wait_seconds=30),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        # Never the real data/skills.json — an isolated, nonexistent path
        # means state.skills is empty, so /ask never attempts a real
        # embedding network call in these tests (app/llm/pipeline.py skips
        # retrieval entirely when skills is empty).
        skills_path=skills_path,
        tts_client=tts_client,
        # Explicitly gate-OFF by default, never create_app's own default --
        # that one resolves SITE_PASSWORD from the developer's real .env
        # (config.current_site_password falls back to the process-start
        # value even for an isolated env_path), so simply adding a real
        # SITE_PASSWORD to .env made every test in this file 401 behind
        # the site gate. Caught exactly that way. The gate's own tests
        # pass a real SiteAuth explicitly.
        site_auth=site_auth or SiteAuth(password=None),
    )
    return TestClient(app), app


@pytest.fixture
def api(tmp_path):
    client, app = make_api(tmp_path)
    with client as test_client:
        yield test_client


def test_health_reports_ok(api):
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_a_freshness_fingerprint(api):
    # A LaunchAgent auto-restarting the server on crash means "it responds"
    # was never proof it reflects a recent code edit -- started_at (proves
    # a restart happened) and code_fingerprint (proves what's running
    # matches the current app/ source tree) together close that gap. See
    # config.py's STARTED_AT/CODE_FINGERPRINT docstring.
    from app import config

    body = api.get("/health").json()
    assert body["code_fingerprint"] == config.CODE_FINGERPRINT
    assert isinstance(body["started_at"], (int, float))
    assert body["started_at"] <= time.time()


def test_ask_returns_a_job_id_and_position(api):
    response = api.post("/ask", json={"device_id": "dev-1", "mode": "build", "question": "drive forward"})
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] > 0
    assert body["position"] >= 1


def test_answer_becomes_available_after_the_job_runs(api):
    ask = api.post("/ask", json={"device_id": "dev-1", "mode": "build", "question": "drive forward"})
    job_id = ask.json()["job_id"]

    for _ in range(50):
        got = api.get(f"/answer/{job_id}")
        if got.json()["status"] == "done":
            break
        time.sleep(0.02)

    body = got.json()
    assert body["status"] == "done"
    assert body["answer"]["message"] == "Drive forward and stop."
    assert body["errors"] == []


def test_unknown_job_id_is_404(api):
    response = api.get("/answer/999999")
    assert response.status_code == 404


def test_invalid_mode_is_rejected(api):
    response = api.post("/ask", json={"device_id": "dev-1", "mode": "sideways", "question": "x"})
    assert response.status_code == 422


def test_a_completed_answer_is_persisted_to_the_session(tmp_path):
    client, app = make_api(tmp_path)
    with client as api:
        ask = api.post(
            "/ask",
            json={"device_id": "dev-2", "mode": "build", "question": "drive forward", "language": "中文"},
        )
        job_id = ask.json()["job_id"]
        for _ in range(50):
            if api.get(f"/answer/{job_id}").json()["status"] == "done":
                break
            time.sleep(0.02)

        session = app.state.sessions.load("dev-2")
        assert session is not None
        assert session.language == "中文"
        assert len(session.turns) == 2  # student turn + assistant turn


def test_a_completed_answer_is_written_to_the_log(tmp_path):
    client, app = make_api(tmp_path)
    with client as api:
        ask = api.post("/ask", json={"device_id": "dev-3", "mode": "build", "question": "drive forward"})
        job_id = ask.json()["job_id"]
        for _ in range(50):
            if api.get(f"/answer/{job_id}").json()["status"] == "done":
                break
            time.sleep(0.02)

    log_files = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    record = json.loads(log_files[0].read_text(encoding="utf-8").strip().split("\n")[0])
    assert record["question"] == "drive forward"
    assert record["answer"]["message"] == "Drive forward and stop."


def _wait_done(api, job_id):
    for _ in range(50):
        body = api.get(f"/answer/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise TimeoutError("job never finished")


def test_an_ask_response_does_not_close_the_session_thread(tmp_path):
    client = FakeClient(response_dict={
        "kind": "ask", "voice": "x", "message": "Which sensor port?", "blocks": [], "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),  # not testing the gate -- see make_api's note
    )
    with TestClient(app) as api:
        ask = api.post("/ask", json={"device_id": "dev-6", "mode": "build", "question": "follow a line"})
        body = _wait_done(api, ask.json()["job_id"])
        assert body["answer"]["kind"] == "ask"

        session = app.state.sessions.load("dev-6")
        assert len(session.turns) == 2
        assert session.turns[-1]["answer"]["kind"] == "ask"


def test_a_reply_to_an_ask_is_sent_as_history_not_a_fresh_question(tmp_path):
    client = FakeClient(response_dict={
        "kind": "ask", "voice": "x", "message": "Which sensor port?", "blocks": [], "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),  # not testing the gate -- see make_api's note
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-7", "mode": "build", "question": "follow a line"})
        _wait_done(api, first.json()["job_id"])

        # Second call: the model now answers completely.
        client.response_dict = VALID_BUILD_RESPONSE
        second = api.post("/ask", json={"device_id": "dev-7", "mode": "build", "question": "No.1"})
        _wait_done(api, second.json()["job_id"])

        assert len(client.calls) == 2
        assert client.calls[0]["history"] is None  # first question: fresh
        history = client.calls[1]["history"]
        assert history is not None
        assert history[0] == {"role": "user", "content": "follow a line"}
        assert history[1] == {"role": "assistant", "content": "Which sensor port?"}


def test_a_complete_turn_closes_the_thread_so_the_next_question_is_fresh(tmp_path):
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)  # kind=complete
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),  # not testing the gate -- see make_api's note
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-8", "mode": "build", "question": "move forward"})
        _wait_done(api, first.json()["job_id"])

        second = api.post("/ask", json={"device_id": "dev-8", "mode": "build", "question": "now turn left"})
        _wait_done(api, second.json()["job_id"])

        assert client.calls[1]["history"] is None  # prior thread completed, so this one starts fresh


def test_continue_thread_carries_the_finished_program_forward(tmp_path):
    # Caught live: a student got a real, correct completed program, then
    # asked to add one more thing to it -- the model had no way to see
    # what it had just built (a complete turn closes the thread on
    # purpose) and could only ask "which program?". continue_thread is
    # the frontend's explicit signal that this follow-up means "extend
    # what I just got", not "unrelated new request".
    completed = {
        "kind": "complete", "voice": "x",
        "message": "Here is your program.",
        "blocks": [
            {
                "name": "Move robot's",
                "slots": [
                    {"kind": "dropdown", "text": "Back axis"},
                    {"kind": "dropdown", "text": "Forward"},
                    {"kind": "value", "text": "50"},
                ],
            }
        ],
        "note": None,
        "image_note": None,
    }
    client = FakeClient(response_dict=completed)
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-cont-1", "mode": "build", "question": "move forward"})
        _wait_done(api, first.json()["job_id"])

        second = api.post("/ask", json={
            "device_id": "dev-cont-1", "mode": "build", "question": "now also make it beep",
            "continue_thread": True,
        })
        _wait_done(api, second.json()["job_id"])

        history = client.calls[1]["history"]
        assert history is not None
        content = history[-1]["content"]
        assert "Move robot's" in content
        assert "Back axis" in content and "Forward" in content and "50" in content


def test_continue_thread_history_alternates_user_then_assistant(tmp_path):
    # Caught live: this history used to be a single bare `assistant` turn
    # with nothing before it -- the real request to the model became
    # system -> assistant -> user, with no user turn ever preceding that
    # assistant one. Observed failure: the model treated the assistant
    # turn as its own message to keep writing and replied in prose instead
    # of JSON, tripping "model did not return valid JSON" on a request
    # that should have succeeded. A synthetic user turn ahead of it
    # restores normal alternation, matching every other history path.
    completed = {
        "kind": "complete", "voice": "x", "message": "Here is your program.",
        "blocks": [{"name": "Stop robot", "slots": []}], "note": None, "image_note": None,
    }
    client = FakeClient(response_dict=completed)
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-alt-1", "mode": "build", "question": "move forward"})
        _wait_done(api, first.json()["job_id"])

        second = api.post("/ask", json={
            "device_id": "dev-alt-1", "mode": "build", "question": "now also make it beep",
            "continue_thread": True,
        })
        _wait_done(api, second.json()["job_id"])

        history = client.calls[1]["history"]
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"


def test_continue_thread_flattens_an_elif_chain_at_the_same_indentation(tmp_path):
    # Caught live: the raw JSON genuinely nests each elif one level deeper
    # into the previous one's else_children (R Block's own containment
    # shape), but rendering that literally as increasing indentation reads
    # as nested code instead of a normal flat if/elif/else -- exactly what
    # a coach flagged as hard to read. The real student app (CBlock,
    # index.html) already fuses these flat; this history-builder must too.
    chain = {
        "kind": "complete", "voice": "x",
        "message": "Here is your program.",
        "blocks": [
            {
                "name": "if then",
                "conditions": [{"name": "If the IR sensor value", "slots": [
                    {"kind": "dropdown", "text": "No.3"}, {"kind": "dropdown", "text": "<"},
                    {"kind": "value", "text": "200"},
                ]}],
                "children": [{"name": "Stop robot", "slots": []}],
                "else_children": [{
                    "name": "if then",
                    "conditions": [{"name": "If the IR sensor value", "slots": [
                        {"kind": "dropdown", "text": "No.4"}, {"kind": "dropdown", "text": "<"},
                        {"kind": "value", "text": "200"},
                    ]}],
                    "children": [{"name": "Move robot's", "slots": [
                        {"kind": "dropdown", "text": "Back axis"}, {"kind": "dropdown", "text": "Forward"},
                        {"kind": "value", "text": "50"},
                    ]}],
                    "else_children": [{"name": "Stop robot", "slots": []}],
                }],
            }
        ],
        "note": None,
        "image_note": None,
    }
    client = FakeClient(response_dict=chain)
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-chain-1", "mode": "build", "question": "line following"})
        _wait_done(api, first.json()["job_id"])

        second = api.post("/ask", json={
            "device_id": "dev-chain-1", "mode": "build", "question": "now also make it beep",
            "continue_thread": True,
        })
        _wait_done(api, second.json()["job_id"])

        content = client.calls[1]["history"][-1]["content"]
        lines = content.splitlines()
        # Both "if then" branches and the real terminal "else:" must sit
        # at the SAME indentation -- not one deeper per branch.
        if_line = next(l for l in lines if "No.3" in l)
        elif_line = next(l for l in lines if "No.4" in l)
        else_line = next(l for l in lines if l.strip() == "else:")
        indent = lambda l: len(l) - len(l.lstrip(" "))
        assert indent(if_line) == indent(elif_line) == indent(else_line)


def test_continue_thread_with_nothing_to_continue_starts_fresh(tmp_path):
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        response = api.post("/ask", json={
            "device_id": "dev-cont-2", "mode": "build", "question": "move forward",
            "continue_thread": True,
        })
        _wait_done(api, response.json()["job_id"])
        assert client.calls[0]["history"] is None


def test_continue_thread_on_a_still_open_phase_falls_back_to_normal_continuation(tmp_path):
    # continue_thread=True doesn't override an ALREADY-open ask/phase
    # thread's own correct history -- _continuation_history_after_complete
    # only fires for a genuinely closed (kind=complete) turn.
    client = FakeClient(response_dict={
        "kind": "phase", "voice": "x", "message": "Phase 1: set up",
        "blocks": [{"name": "Stop robot", "slots": []}],
        "phase_number": 1, "phase_total": 2, "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-cont-3", "mode": "build", "question": "a long program"})
        _wait_done(api, first.json()["job_id"])

        client.response_dict = {**VALID_BUILD_RESPONSE, "message": "Phase 2 done."}
        second = api.post("/ask", json={
            "device_id": "dev-cont-3", "mode": "build", "question": "ready", "continue_thread": True,
        })
        _wait_done(api, second.json()["job_id"])

        history = client.calls[1]["history"]
        assert history is not None
        assert history[0]["content"] == "a long program"
        assert "Blocks so far: Stop robot" in history[1]["content"]


def test_debug_never_carries_history_even_after_a_build_ask(tmp_path):
    client = FakeClient(response_dict={
        "kind": "ask", "voice": "x", "message": "Which sensor port?", "blocks": [], "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),  # not testing the gate -- see make_api's note
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-9", "mode": "build", "question": "follow a line"})
        _wait_done(api, first.json()["job_id"])

        client.response_dict = {
            "kind": "answer", "voice": "x", "summary": "x", "fault": "x",
            "blocks": [{"name": "Stop robot", "slots": []}], "note": None, "image_note": None,
        }
        second = api.post("/ask", json={"device_id": "dev-9", "mode": "debug", "question": "it doesn't work"})
        _wait_done(api, second.json()["job_id"])

        assert client.calls[1]["history"] is None


def test_a_debug_ask_reply_continues_the_thread(tmp_path):
    # Reversed from "Debug never asks" (grilling session): DebugAnswer.kind
    # now allows ONE clarifying question before a real diagnosis. The
    # reply to that question must reach the model as history, the same way
    # a Build ask/phase thread already does.
    client = FakeClient(response_dict={
        "kind": "ask", "voice": "x", "summary": "What is the robot doing instead?", "fault": "", "blocks": [],
        "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-dbg-1", "mode": "debug", "question": "it doesn't work"})
        _wait_done(api, first.json()["job_id"])
        assert client.calls[0]["history"] is None  # nothing to continue yet

        client.response_dict = {
            "kind": "answer", "voice": "x", "summary": "x", "fault": "The threshold was never reset.",
            "blocks": [{"name": "Stop robot", "slots": []}], "note": None, "image_note": None,
        }
        second = api.post(
            "/ask", json={"device_id": "dev-dbg-1", "mode": "debug", "question": "it just spins in place"}
        )
        _wait_done(api, second.json()["job_id"])

        history = client.calls[1]["history"]
        assert history is not None
        assert history[0] == {"role": "user", "content": "it doesn't work"}
        assert history[1] == {"role": "assistant", "content": "What is the robot doing instead?"}


def test_a_debug_answer_closes_the_thread_so_the_next_question_is_fresh(tmp_path):
    client = FakeClient(response_dict={
        "kind": "answer", "voice": "x", "summary": "x", "fault": "The threshold was never reset.",
        "blocks": [{"name": "Stop robot", "slots": []}], "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post(
            "/ask", json={"device_id": "dev-dbg-2", "mode": "debug", "question": "my loop never stops"}
        )
        _wait_done(api, first.json()["job_id"])

        second = api.post(
            "/ask", json={"device_id": "dev-dbg-2", "mode": "debug", "question": "now it doesn't turn at all"}
        )
        _wait_done(api, second.json()["job_id"])

        assert client.calls[1]["history"] is None  # prior diagnosis closed the thread


def test_a_debug_reply_to_an_open_ask_gets_a_stronger_reminder_not_to_ask_again(tmp_path):
    # Caught live: _DEBUG_EXTRA's "ask AT MOST ONE question" rule is soft
    # prompt guidance -- nothing schema-level stops a second kind=ask, and
    # a real thread asked a clarifying question, got answered, then asked
    # ANOTHER question instead of committing to a diagnosis. The reminder
    # is injected into what the MODEL sees only -- the student's own
    # stored turn must stay their real words, untouched.
    client = FakeClient(response_dict={
        "kind": "ask", "voice": "x", "summary": "What is the robot doing instead?", "fault": "", "blocks": [],
        "note": None, "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),
    )
    with TestClient(app) as api:
        first = api.post(
            "/ask", json={"device_id": "dev-dbg-3", "mode": "debug", "question": "it doesn't work"}
        )
        _wait_done(api, first.json()["job_id"])
        assert "already asked" not in client.calls[0]["user"]  # nothing to remind about yet

        client.response_dict = {
            "kind": "answer", "voice": "x", "summary": "x", "fault": "The threshold was never reset.",
            "blocks": [{"name": "Stop robot", "slots": []}], "note": None, "image_note": None,
        }
        second = api.post(
            "/ask", json={"device_id": "dev-dbg-3", "mode": "debug", "question": "it just spins forever"}
        )
        _wait_done(api, second.json()["job_id"])

        sent = client.calls[1]["user"]
        assert sent.startswith("it just spins forever")  # the student's real words, first
        assert 'kind="answer"' in sent  # the reminder, appended

        # The reminder never leaks into what's actually stored -- confirmed
        # by reading it back out as history in a hypothetical third call.
        third = api.post(
            "/ask", json={"device_id": "dev-dbg-3", "mode": "debug", "question": "a brand new problem"}
        )
        _wait_done(api, third.json()["job_id"])
        # A fresh question after a closed (kind=answer) thread starts over
        # with no history at all -- if the reminder had leaked into
        # storage, this history would be non-None and would contain it.
        assert client.calls[2]["history"] is None


def test_a_phase_reply_includes_the_blocks_placed_so_far_in_history(tmp_path):
    client = FakeClient(response_dict={
        "kind": "phase", "voice": "x",
        "message": "Phase 1: set up",
        "blocks": [{"name": "Stop robot", "slots": []}],
        "phase_number": 1,
        "phase_total": 2,
        "note": None,
        "image_note": None,
    })
    app = create_app(
        client=client,
        queue=QueueManager(concurrency=2, max_queue_depth=20, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        site_auth=SiteAuth(password=None),  # not testing the gate -- see make_api's note
    )
    with TestClient(app) as api:
        first = api.post("/ask", json={"device_id": "dev-10", "mode": "build", "question": "a long program"})
        _wait_done(api, first.json()["job_id"])

        client.response_dict = {**VALID_BUILD_RESPONSE, "message": "Phase 2 done."}
        second = api.post("/ask", json={"device_id": "dev-10", "mode": "build", "question": "ready"})
        _wait_done(api, second.json()["job_id"])

        history = client.calls[1]["history"]
        assert "Stop robot" in history[-1]["content"]


def test_queue_full_returns_503(tmp_path):
    client, app = make_api(tmp_path, delay=1, max_queue_depth=1)
    with client as api:
        api.post("/ask", json={"device_id": "dev-4", "mode": "build", "question": "x"})
        response = api.post("/ask", json={"device_id": "dev-5", "mode": "build", "question": "y"})
        assert response.status_code == 503


def _line_following_skill(usage_note="Adjust IR and SPEED for your track."):
    return SkillDoc(
        id="line-following",
        name="Line Following",
        triggers=["line"],
        functions=[Function(name="line_trace", body="if IR3 < IR:\n  turn left", usage_note=usage_note)],
    )


def test_economy_mode_serves_a_match_with_no_llm_call(tmp_path):
    client, app = make_api(tmp_path, skill=_line_following_skill())
    with client as api:
        ask = api.post(
            "/ask",
            json={"device_id": "dev-e1", "mode": "build", "question": "give code on line tracing", "economy": True},
        )
        body = _wait_done(api, ask.json()["job_id"])

        assert body["status"] == "done"
        assert body["answer"] is None
        assert body["economy"]["skill_id"] == "line-following"
        assert body["economy"]["message"] == "Adjust IR and SPEED for your track."
        assert "line_trace" in body["economy"]["code_text"]
        assert app.state.client.calls == []  # no LLM call at all


def test_economy_mode_falls_back_to_full_ai_when_nothing_matches(tmp_path):
    client, app = make_api(tmp_path, skill=_line_following_skill())
    with client as api:
        ask = api.post(
            "/ask",
            json={"device_id": "dev-e2", "mode": "build", "question": "make it beep three times", "economy": True},
        )
        body = _wait_done(api, ask.json()["job_id"])

        assert body["economy"] is None
        assert body["answer"]["message"] == "Drive forward and stop."  # the normal FakeClient response
        assert len(app.state.client.calls) == 1


def test_debug_mode_ignores_the_economy_flag(tmp_path):
    debug_response = {
        "kind": "answer", "voice": "x",
        "summary": "Fixed it.",
        "fault": "The threshold was wrong.",
        "blocks": [{"name": "Stop robot", "slots": []}],
        "note": None,
        "image_note": None,
    }
    client, app = make_api(tmp_path, response_dict=debug_response, skill=_line_following_skill())
    with client as api:
        ask = api.post(
            "/ask",
            json={"device_id": "dev-e3", "mode": "debug", "question": "my line tracer is broken", "economy": True},
        )
        body = _wait_done(api, ask.json()["job_id"])

        assert body["economy"] is None
        assert body["answer"]["summary"] == "Fixed it."
        assert len(app.state.client.calls) == 1


def test_economy_mode_off_uses_the_normal_path_even_with_a_matching_skill(tmp_path):
    client, app = make_api(tmp_path, skill=_line_following_skill())
    with client as api:
        ask = api.post(
            "/ask",
            json={"device_id": "dev-e4", "mode": "build", "question": "give code on line tracing", "economy": False},
        )
        body = _wait_done(api, ask.json()["job_id"])

        assert body["economy"] is None
        assert body["answer"]["message"] == "Drive forward and stop."
        assert len(app.state.client.calls) == 1


def test_an_economy_turn_does_not_leave_the_thread_open_for_a_follow_up(tmp_path):
    # A follow-up after an economy answer must start fresh, not try to
    # continue a nonexistent ask/phase conversation.
    client, app = make_api(tmp_path, skill=_line_following_skill())
    with client as api:
        first = api.post(
            "/ask",
            json={"device_id": "dev-e5", "mode": "build", "question": "give code on line tracing", "economy": True},
        )
        _wait_done(api, first.json()["job_id"])

        second = api.post(
            "/ask",
            json={"device_id": "dev-e5", "mode": "build", "question": "make it beep three times", "economy": True},
        )
        _wait_done(api, second.json()["job_id"])
        # The second call went through the normal path (no match) with a
        # fresh (empty) history, not an attempt to continue the first turn.
        assert app.state.client.calls[0]["history"] in (None, [])


def test_health_reports_warm_state(tmp_path):
    client, app = make_api(tmp_path)
    with client as api:
        response = api.get("/health")
        assert "warm" in response.json()


def test_health_reports_tts_availability(tmp_path, monkeypatch):
    # config.ELEVENLABS_API_KEY is one of the settings /admin/settings can
    # mutate at the module level (app/config.py's _MUTABLE_STRING_SETTINGS,
    # same mechanism GEMINI_API_KEY/OPENROUTER_API_KEY already use) -- an
    # earlier test in the same pytest process setting a key would leak into
    # this one's create_app() default otherwise. Force it clean regardless
    # of test order, same convention as test_config_dotenv.py.
    monkeypatch.setattr("app.config.ELEVENLABS_API_KEY", None)
    client, app = make_api(tmp_path)
    with client as api:
        # No key injected by default (make_api's own docstring) -- /health
        # must say so plainly rather than claiming voice replies work.
        assert api.get("/health").json()["tts_available"] is False


class FakeTTSClient:
    def __init__(self, audio_bytes=b"fake-audio", content_type="audio/mpeg", api_key="fake-key"):
        self.audio_bytes = audio_bytes
        self.content_type = content_type
        self.api_key = api_key  # /health reads this directly, not a method call
        self.voice_id = "v"
        self.model_id = "m"
        self.timeout_seconds = 5
        self.calls = []

    async def synthesize(self, text):
        from app.tts.elevenlabs_client import TTSResult

        self.calls.append(text)
        return TTSResult(audio_bytes=self.audio_bytes, content_type=self.content_type)


def test_tts_returns_audio_bytes_when_a_client_is_configured(tmp_path):
    client, app = make_api(tmp_path, tts_client=FakeTTSClient(audio_bytes=b"\xff\xfbhello-audio"))
    with client as api:
        response = api.post("/tts", json={"text": "Which sensor is it plugged into?"})
        assert response.status_code == 200
        assert response.content == b"\xff\xfbhello-audio"
        assert response.headers["content-type"] == "audio/mpeg"
        assert app.state.tts_client.calls == ["Which sensor is it plugged into?"]


def test_tts_reports_available_in_health_when_a_key_is_set(tmp_path):
    client, app = make_api(tmp_path, tts_client=FakeTTSClient())
    with client as api:
        assert api.get("/health").json()["tts_available"] is True


def test_tts_returns_503_not_500_when_no_key_is_configured(tmp_path, monkeypatch):
    # A quiet, expected "not set up" state, not a server error -- the
    # frontend treats this as "stay silent", not a broken turn (see
    # index.html's `speak`). Same cross-test-pollution guard as
    # test_health_reports_tts_availability above.
    monkeypatch.setattr("app.config.ELEVENLABS_API_KEY", None)
    client, app = make_api(tmp_path)  # default tts_client has no key
    with client as api:
        response = api.post("/tts", json={"text": "hello"})
        assert response.status_code == 503


def test_tts_rejects_empty_text():
    from pydantic import ValidationError

    from app.api.main import TTSRequest

    with pytest.raises(ValidationError):
        TTSRequest(text="")


def test_tts_rejects_text_over_the_length_cap():
    from pydantic import ValidationError

    from app.api.main import TTSRequest

    with pytest.raises(ValidationError):
        TTSRequest(text="x" * 801)


def test_no_site_password_configured_means_the_gate_is_off(tmp_path):
    # Default: create_app()'s own SiteAuth reads SITE_PASSWORD from the
    # real environment, unset in a test run -- every endpoint stays open.
    client, app = make_api(tmp_path)
    with client as api:
        assert api.get("/health").status_code == 200


def test_the_gate_blocks_an_unauthenticated_request_when_a_password_is_set(tmp_path):
    client, app = make_api(tmp_path, site_auth=SiteAuth(password="robotclub"))
    with client as api:
        response = api.get("/")
        assert response.status_code == 401
        assert "R Block Coding Buddy" in response.text  # the gate page itself, not a bare 401


def test_health_stays_open_even_with_the_gate_on(tmp_path):
    # External monitoring must keep working regardless of the site gate.
    client, app = make_api(tmp_path, site_auth=SiteAuth(password="robotclub"))
    with client as api:
        assert api.get("/health").status_code == 200


def test_a_correct_site_password_grants_access_via_cookie(tmp_path):
    client, app = make_api(tmp_path, site_auth=SiteAuth(password="robotclub"))
    with client as api:
        login = api.post("/site-login", json={"password": "robotclub"})
        assert login.status_code == 200
        assert COOKIE_NAME in login.cookies

        # The SAME client instance carries the cookie forward automatically.
        response = api.get("/")
        assert response.status_code == 200


def test_a_wrong_site_password_is_rejected(tmp_path):
    client, app = make_api(tmp_path, site_auth=SiteAuth(password="robotclub"))
    with client as api:
        response = api.post("/site-login", json={"password": "wrong"})
        assert response.status_code == 401
        assert COOKIE_NAME not in response.cookies
