"""The admin endpoints through the real HTTP surface: login, settings
read/write, and the security-relevant edges (no auth, wrong password,
switching to gemini with no key). `env_path` is always a tmp_path file here
-- never the real project .env, see app/api/main.py's create_app."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from app.admin.auth import AdminAuth
from app.site_auth import SiteAuth
from app.api.main import create_app
from app.llm.ollama_client import ChatResult
from app.queue.manager import QueueManager
from app.session.log import LogWriter
from app.session.store import FileSessionStore


class FakeClient:
    def __init__(self, model="fake-model"):
        self.model = model

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        return ChatResult(text=json.dumps({"kind": "complete", "voice": "x", "message": "x", "blocks": []}), model=self.model)


class FakeClientWithRealBlocks:
    """Unlike FakeClient (kind=complete but `blocks: []`, which
    render_demo_blocks correctly treats as "nothing to render"), this
    always returns a real block -- for tests that need save_skill's
    render_demo_blocks step to actually produce something."""

    def __init__(self, model="fake-model"):
        self.model = model

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        body = {"kind": "complete", "voice": "x", "message": "x", "blocks": [{"name": "Stop robot", "slots": []}]}
        return ChatResult(text=json.dumps(body), model=self.model)


class FakeEmbedder:
    """Stands in for OllamaEmbedder so /admin/skills tests don't make a real
    network call on every save (compute_embedding calls this)."""

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


def make_admin_app(tmp_path, password="right-password", restart_fn=None):
    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_BACKEND=local\nOLLAMA_MODEL=gemma4:12b-it-qat\n", encoding="utf-8")
    app = create_app(
        client=FakeClient(),
        queue=QueueManager(concurrency=1, max_queue_depth=5, min_wait_seconds=1, max_wait_seconds=1),
        sessions=FileSessionStore(directory=tmp_path / "sessions", ttl_seconds=3600),
        log=LogWriter(directory=tmp_path / "logs"),
        admin_auth=AdminAuth(password=password),
        # Not testing the site gate here -- without this, create_app
        # resolves SITE_PASSWORD from the real .env and 401s everything.
        site_auth=SiteAuth(password=None),
        env_path=env_path,
        skills_path=tmp_path / "skills.json",
        prompts_path=tmp_path / "prompts.json",
        embedder=FakeEmbedder(),
        # Tests that don't exercise /admin/restart never call it, so the
        # real default (os.execv, replacing the TEST RUNNER's own process)
        # never fires -- but never risk that by accident: every test here
        # gets a harmless no-op unless a restart test explicitly overrides
        # it with something it can assert against.
        restart_fn=restart_fn or (lambda: None),
    )
    return TestClient(app), app, env_path


def skill_payload(id="line-following"):
    return {
        "id": id,
        "name": "Line Following",
        "triggers": ["line", "junction"],
        "intro": "Use multiple sensors.",
        "functions": [
            {"name": "line_trace", "body": "if IR3 < IR:\n  turn left\nelse:\n  move forward"},
            {"name": "FORWARD", "body": "move forward\nwait 0.15s"},
        ],
    }


def test_login_with_the_right_password_returns_a_token(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        response = api.post("/admin/login", json={"password": "right-password"})
        assert response.status_code == 200
        assert "token" in response.json()


def test_login_with_the_wrong_password_is_rejected(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        response = api.post("/admin/login", json={"password": "wrong"})
        assert response.status_code == 401


def test_login_is_rejected_when_no_admin_password_is_configured(tmp_path):
    api, app, _ = make_admin_app(tmp_path, password=None)
    with api:
        response = api.post("/admin/login", json={"password": "anything"})
        assert response.status_code == 401


def test_settings_requires_authentication(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        response = api.get("/admin/settings")
        assert response.status_code == 401


def test_settings_rejects_a_bogus_token(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        response = api.get("/admin/settings", headers={"Authorization": "Bearer not-a-real-token"})
        assert response.status_code == 401


def test_a_logged_in_admin_can_read_current_settings(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.get("/admin/settings", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["backend"] == "local"
        assert body["ollama_model"] == "gemma4:12b-it-qat"
        assert body["gemini_api_key_set"] is False


def test_the_raw_gemini_key_is_never_returned(tmp_path):
    api, app, env_path = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/settings",
            json={"backend": "gemini", "gemini_api_key": "super-secret-key-value"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response = api.get("/admin/settings", headers={"Authorization": f"Bearer {token}"})
        assert "super-secret-key-value" not in response.text
        assert response.json()["gemini_api_key_set"] is True


def test_switching_to_gemini_without_a_key_is_rejected(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/settings", json={"backend": "gemini"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 400
        assert "api key" in response.json()["detail"].lower()


def test_switching_to_gemini_with_a_key_hot_swaps_the_client(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        assert isinstance(app.state.client, FakeClient)

        response = api.post(
            "/admin/settings",
            json={"backend": "gemini", "gemini_api_key": "a-real-key", "gemini_model": "gemini-2.5-flash"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["backend"] == "gemini"
        # The live app.state.client actually changed -- not just the reported settings.
        from app.llm.gemini_client import GeminiClient

        assert isinstance(app.state.client, GeminiClient)
        assert app.state.client.model == "gemini-2.5-flash"


def test_switching_to_openrouter_without_a_key_is_rejected(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/settings", json={"backend": "openrouter"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 400
        assert "api key" in response.json()["detail"].lower()


def test_switching_to_openrouter_with_a_key_hot_swaps_the_client(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        assert isinstance(app.state.client, FakeClient)

        response = api.post(
            "/admin/settings",
            json={"backend": "openrouter", "openrouter_api_key": "a-real-key", "openrouter_model": "openrouter/free"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["backend"] == "openrouter"
        assert response.json()["openrouter_api_key_set"] is True
        from app.llm.openrouter_client import OpenRouterClient

        assert isinstance(app.state.client, OpenRouterClient)
        assert app.state.client.model == "openrouter/free"


def test_the_raw_openrouter_key_is_never_returned(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/settings",
            json={"backend": "openrouter", "openrouter_api_key": "super-secret-or-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response = api.get("/admin/settings", headers={"Authorization": f"Bearer {token}"})
        assert "super-secret-or-key" not in response.text
        assert response.json()["openrouter_api_key_set"] is True


def test_elevenlabs_key_is_independent_of_backend(tmp_path):
    # Unlike gemini/openrouter, setting the ElevenLabs key never needs (or
    # is blocked by) a `backend` switch -- it's TTS for the assistant's own
    # questions, not one of the Build/Debug model choices.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/settings",
            json={"backend": "local", "elevenlabs_api_key": "a-real-elevenlabs-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["elevenlabs_api_key_set"] is True
        assert app.state.tts_client.api_key == "a-real-elevenlabs-key"


def test_elevenlabs_voice_id_hot_swaps_independently_of_the_key(tmp_path):
    # An admin picking a different voice from the dropdown shouldn't also
    # have to re-enter the API key -- both fields are independently
    # optional on AdminSettingsUpdate, and either one alone must still
    # trigger the hot-swap (see the "or" in update_admin_settings).
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/settings",
            json={"backend": "local", "elevenlabs_api_key": "a-real-elevenlabs-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response = api.post(
            "/admin/settings",
            json={"backend": "local", "elevenlabs_voice_id": "l32B8XDoylOsZKiSdfhE"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["elevenlabs_voice_id"] == "l32B8XDoylOsZKiSdfhE"
        # The key set on the earlier call must survive -- switching voice
        # alone must not silently drop it.
        assert app.state.tts_client.api_key == "a-real-elevenlabs-key"
        assert app.state.tts_client.voice_id == "l32B8XDoylOsZKiSdfhE"


def test_the_raw_elevenlabs_key_is_never_returned(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/settings",
            json={"backend": "local", "elevenlabs_api_key": "super-secret-11labs-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response = api.get("/admin/settings", headers={"Authorization": f"Bearer {token}"})
        assert "super-secret-11labs-key" not in response.text
        assert response.json()["elevenlabs_api_key_set"] is True


def test_settings_changes_are_persisted_to_the_env_file(tmp_path):
    api, app, env_path = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/settings",
            json={"backend": "gemini", "gemini_api_key": "persisted-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        text = env_path.read_text(encoding="utf-8")
        assert "MODEL_BACKEND=gemini" in text
        assert "GEMINI_API_KEY=persisted-key" in text


def test_switching_back_to_local_keeps_the_existing_gemini_key_set_flag(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/settings",
            json={"backend": "gemini", "gemini_api_key": "a-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response = api.post(
            "/admin/settings", json={"backend": "local"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["gemini_api_key_set"] is True


def test_skills_list_requires_authentication(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        assert api.get("/admin/skills").status_code == 401


def test_a_new_skill_can_be_saved_and_then_listed(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills", json=skill_payload(), headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "line-following"
        assert body["updated_at"]

        listed = api.get("/admin/skills", headers={"Authorization": f"Bearer {token}"}).json()
        assert len(listed) == 1
        assert listed[0]["name"] == "Line Following"
        assert listed[0]["functions"][0]["name"] == "line_trace"
        assert "turn left" in listed[0]["functions"][0]["body"]


def test_saving_a_skill_renders_and_persists_real_blocks(tmp_path):
    # app/skills/demo.py's one exception to Economy mode's "no LLM call per
    # request" rule: the render happens once, at save time, and is stored
    # on the skill doc (not just returned in this response) so Economy
    # mode never needs to call the model again for it.
    api, app, _ = make_admin_app(tmp_path)
    app.state.client = FakeClientWithRealBlocks()
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills", json=skill_payload(), headers={"Authorization": f"Bearer {token}"}
        )
        preview = response.json()["preview"]
        assert preview["student_render"]
        assert preview["student_render"][0]["p"] == [["t", "Stop robot"]]

    from app.skills import store as skills_store

    doc = skills_store.get("line-following", path=app.state.skills_path)
    assert doc.demo_render == preview["student_render"]


def test_saving_a_skill_degrades_gracefully_when_the_model_backend_is_down(tmp_path):
    class DownClient:
        async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
            from app.llm.ollama_client import ModelUnavailable

            raise ModelUnavailable("backend is down")

    api, app, _ = make_admin_app(tmp_path)
    app.state.client = DownClient()
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills", json=skill_payload(), headers={"Authorization": f"Bearer {token}"}
        )
        # The save itself still succeeds -- same tolerance as the embedder
        # being down -- just with no cached render this time.
        assert response.status_code == 200
        assert response.json()["preview"]["student_render"] == []


def test_saving_a_skill_computes_an_embedding_on_disk(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post("/admin/skills", json=skill_payload(), headers={"Authorization": f"Bearer {token}"})

    from app.skills import store as skills_store

    doc = skills_store.get("line-following", path=app.state.skills_path)
    assert doc.embedding == [1.0, 0.0]  # from FakeEmbedder


def test_saving_a_skill_again_replaces_it_not_duplicates(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        api.post("/admin/skills", json=skill_payload(), headers=headers)
        payload = skill_payload()
        payload["name"] = "Line Following v2"
        api.post("/admin/skills", json=payload, headers=headers)

        listed = api.get("/admin/skills", headers=headers).json()
        assert len(listed) == 1
        assert listed[0]["name"] == "Line Following v2"


def test_an_invalid_skill_id_is_rejected(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        payload = skill_payload(id="Not A Valid Id!")
        response = api.post("/admin/skills", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 422


def test_deleting_a_skill_removes_it(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        api.post("/admin/skills", json=skill_payload(), headers=headers)

        response = api.delete("/admin/skills/line-following", headers=headers)
        assert response.status_code == 200
        assert api.get("/admin/skills", headers=headers).json() == []


def test_deleting_an_unknown_skill_is_a_404(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.delete("/admin/skills/nope", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 404


def test_a_long_but_realistic_intro_round_trips_through_save_and_list(tmp_path):
    # Caught live against the real skills.json: a skill's `intro` field can
    # legitimately run long (the line-following skill's own "REQUIRED
    # PROGRAM SHAPE" rules ran to ~2000 characters) -- SkillDocOut shares
    # the same max_length as SkillDocIn, so a cap tight enough to reject a
    # save also 500s the LIST endpoint outright the moment such a skill
    # exists, with no way to view or edit it again short of hand-editing
    # the JSON file on disk.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        payload = skill_payload()
        payload["intro"] = "x" * 2000
        save = api.post("/admin/skills", json=payload, headers=headers)
        assert save.status_code == 200

        listed = api.get("/admin/skills", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()[0]["intro"]) == 2000


def test_saving_a_skill_returns_a_preview_of_how_it_will_actually_be_used(tmp_path):
    # So an admin can see what a save produced without guessing -- both
    # places this skill-doc is ever actually read: the AI's own context
    # (render_skill) and the Economy-mode student view (build_economy_answer).
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills", json=skill_payload(), headers={"Authorization": f"Bearer {token}"}
        )
        preview = response.json()["preview"]
        assert "line_trace" in preview["ai_context"]
        assert "MAIN LOOP" in preview["ai_context"]  # render_skill's own boilerplate
        assert "turn left" in preview["student_code"]
        assert preview["student_message"]  # falls back to a placeholder, never empty


def test_listing_skills_does_not_include_a_preview(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        api.post("/admin/skills", json=skill_payload(), headers=headers)
        listed = api.get("/admin/skills", headers=headers).json()
        assert listed[0]["preview"] is None


class FakeDraftClient:
    """Returns a fixed, valid SkillDraftOut-shaped JSON regardless of the
    photo -- draft-from-image tests aren't testing the model's vision, only
    that the endpoint wires the call up and returns/validates it."""

    def __init__(self, text=None, model="fake-model"):
        self._text = text or json.dumps(
            {
                "name": "Line Following",
                "intro": "Use four IR sensors together.",
                "triggers": ["line", "junction"],
                "functions": [
                    {"name": "line_trace", "body": "if IR3 < IR:\n  call turn_left\nelse:\n  call forward", "usage_note": ""},
                    {"name": "turn_left", "body": "rotate Left clockwise", "usage_note": "Tune the speed."},
                ],
            }
        )
        self.model = model

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.last_call = {"system": system, "user": user, "schema": schema, "image_base64": image_base64}
        return ChatResult(text=self._text, model=self.model)


def test_draft_from_image_requires_authentication(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        response = api.post("/admin/skills/draft-from-image", json={"image_base64": "abc"})
        assert response.status_code == 401


def test_draft_from_image_returns_a_reviewable_draft_without_saving(tmp_path):
    api, app, env_path = make_admin_app(tmp_path)
    app.state.client = FakeDraftClient()
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills/draft-from-image",
            json={"image_base64": "ZmFrZS1pbWFnZQ==", "note": "Sensors are ports 3-6."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Line Following"
        assert body["functions"][0]["name"] == "line_trace"

        # Nothing was written to the skills store -- this is a draft only.
        assert api.get("/admin/skills", headers={"Authorization": f"Bearer {token}"}).json() == []


def test_draft_from_image_passes_the_photo_and_note_to_the_model(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    fake = FakeDraftClient()
    app.state.client = fake
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post(
            "/admin/skills/draft-from-image",
            json={"image_base64": "ZmFrZS1pbWFnZQ==", "note": "Sensors are ports 3-6."},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert fake.last_call["image_base64"] == "ZmFrZS1pbWFnZQ=="
    assert "Sensors are ports 3-6." in fake.last_call["system"]
    assert "functions" in fake.last_call["schema"]["properties"]


def test_draft_from_image_with_unparseable_output_is_a_502(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    app.state.client = FakeDraftClient(text="not json")
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills/draft-from-image",
            json={"image_base64": "ZmFrZS1pbWFnZQ=="},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 502


def test_draft_from_image_with_the_wrong_shape_is_a_502(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    app.state.client = FakeDraftClient(text=json.dumps({"name": "x"}))  # missing required `functions`
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/skills/draft-from-image",
            json={"image_base64": "ZmFrZS1pbWFnZQ=="},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 502


def test_restart_requires_authentication(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        response = api.post("/admin/restart", json={})
        assert response.status_code == 401


def test_restart_with_no_active_jobs_calls_restart_fn(tmp_path):
    calls = []
    api, app, _ = make_admin_app(tmp_path, restart_fn=lambda: calls.append(1))
    with api:
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/restart", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body == {"restarting": True, "active_jobs": 0}

        time.sleep(0.7)  # the 0.5s delay before restart_fn actually fires
    assert calls == [1]


def test_restart_with_an_active_job_is_a_409_and_does_not_restart(tmp_path):
    calls = []
    api, app, _ = make_admin_app(tmp_path, restart_fn=lambda: calls.append(1))
    with api:
        async def slow_job():
            await asyncio.sleep(1.5)
            return "done"

        app.state.queue.submit(slow_job)

        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/restart", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 409
        assert "1 student request" in response.json()["detail"]

        time.sleep(0.7)
    # Never restarted -- the whole point of the check.
    assert calls == []


def test_restart_with_force_restarts_despite_an_active_job(tmp_path):
    calls = []
    api, app, _ = make_admin_app(tmp_path, restart_fn=lambda: calls.append(1))
    with api:
        async def slow_job():
            await asyncio.sleep(1.5)
            return "done"

        app.state.queue.submit(slow_job)

        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        response = api.post(
            "/admin/restart", json={"force": True}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["restarting"] is True
        assert body["active_jobs"] == 1

        time.sleep(0.7)
    assert calls == [1]


def test_saving_a_skill_updates_the_in_process_cache_used_by_ask(tmp_path):
    # app.state.skills is what app/llm/pipeline.py's retrieval actually
    # reads (app/api/main.py's /ask handler) -- a save must refresh it, not
    # just the on-disk file, or a newly added skill wouldn't be reachable
    # until a restart.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        assert app.state.skills == []
        token = api.post("/admin/login", json={"password": "right-password"}).json()["token"]
        api.post("/admin/skills", json=skill_payload(), headers={"Authorization": f"Bearer {token}"})
        assert len(app.state.skills) == 1
        assert app.state.skills[0].id == "line-following"


def _admin_token(api):
    return api.post("/admin/login", json={"password": "right-password"}).json()["token"]


def test_prompts_requires_authentication(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        assert api.get("/admin/prompts").status_code == 401


def test_a_fresh_install_reports_every_prompt_as_default(tmp_path):
    # No data/prompts.json yet (or an isolated tmp_path one, here) --
    # PromptOverrides()'s all-None default means every field reports as
    # the hardcoded default, exactly matching pre-feature behavior.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        body = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["system_template_is_default"] is True
        assert body["build_extra_is_default"] is True
        assert body["debug_extra_is_default"] is True
        assert body["system_template"] == body["default_system_template"]


def test_saving_a_valid_override_takes_effect_immediately(tmp_path):
    # No restart -- app.state.prompts is what app/llm/pipeline.py's
    # answer() reads on every /ask call, refreshed in-process on save.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        new_template = default["default_system_template"].replace(
            "You help a robotics-club student", "MARKER You help a robotics-club student"
        )
        response = api.post(
            "/admin/prompts",
            json={"system_template": new_template},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["system_template_is_default"] is False
        assert "MARKER" in body["system_template"]
        assert app.state.prompts.system_template == new_template.strip()


def test_saving_an_override_missing_a_required_placeholder_is_rejected(tmp_path):
    # validate_prompt_override, exercised through the real HTTP surface --
    # deleting {directory_context} formats fine but silently drops the
    # block reference from every request, so this must 400, not save.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        broken = default["default_system_template"].replace("{directory_context}", "")
        response = api.post(
            "/admin/prompts",
            json={"system_template": broken},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
        assert "directory_context" in response.json()["detail"]
        # Rejected -- must NOT have been saved.
        assert app.state.prompts.system_template is None


def test_saving_an_override_with_a_stray_brace_is_rejected(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        broken = default["default_system_template"] + " {oops}"
        response = api.post(
            "/admin/prompts",
            json={"system_template": broken},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400


def test_an_empty_string_resets_a_field_to_default(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom = default["default_build_extra"] + "\n- Extra house rule."
        api.post(
            "/admin/prompts", json={"build_extra": custom}, headers={"Authorization": f"Bearer {token}"}
        )
        assert app.state.prompts.build_extra == custom

        reset = api.post(
            "/admin/prompts", json={"build_extra": ""}, headers={"Authorization": f"Bearer {token}"}
        )
        assert reset.status_code == 200
        assert reset.json()["build_extra_is_default"] is True
        assert app.state.prompts.build_extra is None


def test_omitting_a_field_leaves_it_unchanged(tmp_path):
    # None (the field absent from the JSON body) must mean "don't touch
    # this one" -- distinct from "" (reset to default).
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom_debug = default["default_debug_extra"] + "\n- Extra note."
        api.post(
            "/admin/prompts", json={"debug_extra": custom_debug}, headers={"Authorization": f"Bearer {token}"}
        )
        # Save system_template only -- debug_extra must survive untouched.
        api.post(
            "/admin/prompts",
            json={"system_template": default["default_system_template"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert app.state.prompts.debug_extra == custom_debug


def test_a_saved_override_is_persisted_to_disk_and_reloadable(tmp_path):
    from app.prompts import store as prompts_store

    api, app, _env = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom = default["default_debug_extra"] + "\n- Persisted note."
        api.post(
            "/admin/prompts", json={"debug_extra": custom}, headers={"Authorization": f"Bearer {token}"}
        )
        reloaded = prompts_store.load(app.state.prompts_path)
        assert reloaded.debug_extra == custom


def test_set_as_default_promotes_the_active_text_and_clears_the_active_override(tmp_path):
    # "Set as default" -- a teacher's own saved default (PromptOverrides'
    # second layer) is what "Reset to default" reverts to afterwards, not
    # the original hardcoded text.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom = default["default_debug_extra"] + "\n- Our house rule."

        response = api.post(
            "/admin/prompts",
            json={"debug_extra": custom, "debug_extra_set_as_default": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        # Now IS the default -- no active override needed, badge says so.
        assert body["debug_extra_is_default"] is True
        assert body["default_debug_extra_is_custom"] is True
        assert body["default_debug_extra"] == custom
        assert body["debug_extra"] == custom
        assert app.state.prompts.debug_extra is None
        assert app.state.prompts.debug_extra_default == custom


def test_reset_after_set_as_default_goes_back_to_the_custom_default_not_the_original(tmp_path):
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom_default = default["default_build_extra"] + "\n- Our house rule."
        api.post(
            "/admin/prompts",
            json={"build_extra": custom_default, "build_extra_set_as_default": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Now tune away from that default temporarily...
        api.post(
            "/admin/prompts",
            json={"build_extra": custom_default + "\n- One more tweak."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert app.state.prompts.build_extra is not None
        # ...then reset: must land back on the CUSTOM default, not the
        # original hardcoded text.
        reset = api.post(
            "/admin/prompts", json={"build_extra": ""}, headers={"Authorization": f"Bearer {token}"}
        )
        assert reset.json()["build_extra"] == custom_default
        assert reset.json()["build_extra"] != default["default_build_extra"]


def test_set_as_default_with_no_text_promotes_the_currently_active_value(tmp_path):
    # set_as_default alone (no accompanying field text in the SAME request)
    # promotes whatever is already active, not the pre-existing default.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom = default["default_system_template"].replace(
            "You help a robotics-club student", "MARKER You help a robotics-club student"
        )
        api.post(
            "/admin/prompts", json={"system_template": custom}, headers={"Authorization": f"Bearer {token}"}
        )
        response = api.post(
            "/admin/prompts",
            json={"system_template_set_as_default": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert "MARKER" in response.json()["default_system_template"]


def test_set_as_default_with_no_active_text_at_all_promotes_the_original(tmp_path):
    # Called on a completely untouched field -- promotes the ORIGINAL
    # hardcoded text as the new "default" (a harmless no-op in practice,
    # but must not crash or reject).
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        response = api.post(
            "/admin/prompts",
            json={"debug_extra_set_as_default": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["default_debug_extra"] == default["default_debug_extra"]
        assert response.json()["default_debug_extra_is_custom"] is True


def test_restore_original_clears_both_the_active_and_default_overrides(tmp_path):
    # The escape hatch "Set as default" needs -- once a custom default is
    # set, "Reset to default" alone can never reach the ORIGINAL text
    # again. This must, going all the way back to a fresh-install state.
    api, app, _ = make_admin_app(tmp_path)
    with api:
        token = _admin_token(api)
        default = api.get("/admin/prompts", headers={"Authorization": f"Bearer {token}"}).json()
        custom_default = default["default_debug_extra"] + "\n- Our house rule."
        api.post(
            "/admin/prompts",
            json={"debug_extra": custom_default, "debug_extra_set_as_default": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        api.post(
            "/admin/prompts",
            json={"debug_extra": custom_default + "\n- One more tweak."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert app.state.prompts.debug_extra_default == custom_default
        assert app.state.prompts.debug_extra is not None

        response = api.post(
            "/admin/prompts",
            json={"debug_extra_restore_original": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert app.state.prompts.debug_extra is None
        assert app.state.prompts.debug_extra_default is None
        assert response.json()["default_debug_extra"] == default["default_debug_extra"]
        assert response.json()["default_debug_extra_is_custom"] is False
