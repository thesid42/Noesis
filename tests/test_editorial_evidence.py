from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flower_apps"))

from noesis_agents import agent_app  # noqa: E402
from noesis_agents.policy import CAMERA_IDS, safe_round_input  # noqa: E402


MODEL = "test-kimi-model"


def _round() -> dict:
    return {
        "request_id": "req-1", "session_id": "session-1", "epoch": 2,
        "override_epoch": 3, "model_epoch": 4, "model": MODEL,
        "observation_revision": 12, "media_time_s": 8.0,
        "deadline_remaining_ms": 12_000,
        "cameras": [
            {"id": camera, "healthy": True, "speaking": camera == "closeup2",
             "speaker_state": "speaking" if camera == "closeup2" else "silent",
             "energy": 0.4 if camera == "closeup2" else 0.01, "age_ms": 35,
             "source_time_s": 7.9}
            for camera in CAMERA_IDS
        ] + [{"id": "corner", "healthy": True, "age_ms": 30}],
        "program": {"camera_id": "corner", "reason": "HISTORICAL PROGRAM REASON"},
        "recent_events": [],
        "previous_reports": [],
        "editorial_context": {
            "shot_history": [{"camera_id": "closeup1", "start_s": 1.0, "end_s": 5.0,
                              "duration_s": 4.0, "reason": "HISTORICAL SHOT REASON", "source": "model"}],
            "pending_cuts": [{"camera_id": "closeup3", "target_media_time_s": 8.5,
                              "reason": "HISTORICAL CUT REASON"}],
            "perception": {"source_end_s": 7.9, "visual_observations": [
                {"camera_id": "closeup1", "source_time_s": 7.8, "age_ms": 100,
                 "face_count": 0, "face_status": "detected_zero"},
            ]},
        },
    }


def _camera_report(round_data: dict, camera_id: str, response_id: str) -> dict:
    return {
        "request_id": round_data["request_id"], "session_id": round_data["session_id"],
        "epoch": round_data["epoch"], "override_epoch": round_data["override_epoch"],
        "model_epoch": round_data["model_epoch"], "source_revision": round_data["observation_revision"],
        "model": MODEL, "role": "camera", "camera_id": camera_id,
        "agent_id": f"camera-{camera_id}", "response_id": response_id,
        "media_time_s": 7.9,
        "result": {"recommendation": "hold", "confidence": 0.6,
                   "reason": "No affirmative takeover candidate.",
                   "visual": {"person_visibility": "visible", "board_visibility": "uncertain",
                              "activity": "seated", "summary": "A person is seated."}},
    }


def _critic_report(round_data: dict) -> dict:
    return {
        "request_id": round_data["request_id"], "session_id": round_data["session_id"],
        "epoch": round_data["epoch"], "override_epoch": round_data["override_epoch"],
        "model_epoch": round_data["model_epoch"], "source_revision": round_data["observation_revision"],
        "model": MODEL, "role": "critic", "agent_id": "critic", "response_id": "critic-resp",
        "media_time_s": 7.9,
        "result": {"assessment": "steady", "reason": "Audio remains the primary speaker cue."},
    }


def test_prompt_input_removes_program_and_history_reasons_but_keeps_timing_and_ids():
    round_data = _round()
    round_data["previous_reports"] = [{
        **_camera_report(round_data, "closeup1", "old-camera-resp"),
    }]

    safe = safe_round_input(round_data)
    encoded = json.dumps(safe)

    assert "HISTORICAL PROGRAM REASON" not in encoded
    assert "HISTORICAL SHOT REASON" not in encoded
    assert "HISTORICAL CUT REASON" not in encoded
    assert safe["program"] == {"camera_id": "corner"}
    history = safe["editorial_context"]["shot_history"][0]
    assert history["camera_id"] == "closeup1"
    assert history["start_s"] == 1.0 and history["end_s"] == 5.0
    pending = safe["editorial_context"]["pending_cuts_history"][0]
    assert pending == {"camera_id": "closeup3", "target_media_time_s": 8.5}
    assert safe["previous_reports"][0]["response_id"] == "old-camera-resp"
    assert safe["previous_reports"][0]["media_time_s"] == 7.9


def test_critic_gets_prior_camera_reports_only_with_their_media_times(monkeypatch: pytest.MonkeyPatch):
    round_data = _round()
    camera = _camera_report(round_data, "closeup1", "old-camera-resp")
    critic = _critic_report(round_data)
    critic["response_id"] = "old-critic-resp"
    director = {"role": "director", "response_id": "old-director-resp"}
    round_data["previous_reports"] = [camera, critic, director]
    captured: dict = {}

    def fake_infer(_model, instructions, data, _schema_name, _schema, **_kwargs):
        captured.update(instructions=instructions, data=data)
        return {"result": {"assessment": "steady", "reason": "Measured evidence is stable."},
                "response_id": "current-critic", "latency_ms": 5, "input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(agent_app, "_infer", fake_infer)
    monkeypatch.setattr(agent_app, "_http_json", lambda *_a, **_k: {"ok": True})
    agent_app._critic_job({"agent_id": "critic", "role": "critic",
                           "controller_url": agent_app.DEFAULT_CONTROLLER_URL}, {"round": round_data})

    prior = captured["data"]["prior_ai_reports"]
    assert [item["role"] for item in prior] == ["camera"]
    assert prior[0]["response_id"] == "old-camera-resp"
    assert prior[0]["media_time_s"] == 7.9
    assert "old-critic-resp" not in json.dumps(captured["data"])
    assert "face_count of zero is unreliable" in captured["instructions"]
    assert "editorial relevance" in captured["instructions"]


def test_director_receives_one_target_snapshot_and_only_pinned_reports(monkeypatch: pytest.MonkeyPatch):
    round_data = _round()
    round_data["previous_reports"] = [
        _camera_report(round_data, camera, f"old-{camera}") for camera in CAMERA_IDS
    ] + [_critic_report(round_data)]
    reports = [_camera_report(round_data, camera, f"pinned-{camera}") for camera in CAMERA_IDS]
    critic = _critic_report(round_data)
    snapshot = {
        "session": {"id": "session-1", "epoch": 2, "status": "running", "time_s": 8.0},
        "model_epoch": 4, "override_epoch": 3, "model": MODEL,
        "observation_revision": 15,
        "program": {"camera_id": "corner", "reason": "FRESH PROGRAM REASON"},
        "cameras": round_data["cameras"],
    }
    captured: dict = {}
    posts: list[dict] = []

    def fake_http(_url, path, *, payload=None, **_kwargs):
        if path == "/api/agents/snapshot":
            return snapshot
        posts.append(payload)
        return {"ok": True}

    def fake_infer(_model, instructions, data, _schema_name, _schema, **_kwargs):
        captured.update(instructions=instructions, data=data)
        return {"result": {"action": "hold", "camera_id": "corner", "reason": "No affirmative takeover; current shot remains."},
                "response_id": "director-resp", "latency_ms": 8, "input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(agent_app, "_http_json", fake_http)
    monkeypatch.setattr(agent_app, "_infer", fake_infer)
    agent_app._director_job(
        {"agent_id": "director", "role": "director", "controller_url": agent_app.DEFAULT_CONTROLLER_URL},
        {"round": round_data, "camera_reports": reports, "critic_report": critic},
    )

    data = captured["data"]
    assert "cameras" not in data["request"]
    assert "program" not in data["request"]
    assert "editorial_context" not in data["request"]
    assert "previous_reports" not in data["request"]
    assert len(data["fresh_snapshot"]["cameras"]) == len(CAMERA_IDS) + 1
    assert data["fresh_snapshot"]["program"] == {"camera_id": "corner"}
    assert [item["response_id"] for item in data["camera_reports"]] == [f"pinned-{camera}" for camera in CAMERA_IDS]
    assert all(item["media_time_s"] == 7.9 for item in data["camera_reports"])
    assert data["critic_report"]["media_time_s"] == 7.9
    encoded = json.dumps(data)
    assert "FRESH PROGRAM REASON" not in encoded
    assert "HISTORICAL PROGRAM REASON" not in encoded
    assert "old-closeup1" not in encoded
    assert "pinned reports" in captured["instructions"]
    assert "at most 140 characters" in captured["instructions"]


def test_camera_prompt_treats_audio_as_primary_and_still_frame_as_static(monkeypatch: pytest.MonkeyPatch):
    round_data = _round()
    captured: dict = {}

    def fake_image(_round, camera_id):
        return ({"camera_id": camera_id, "frame_time_s": 7.8, "source_time_s": 7.8,
                 "buffer_epoch": 1, "sha256": "a" * 64, "width": 320, "height": 180},
                "data:image/jpeg;base64,AA==")

    def fake_infer(_model, instructions, data, _schema_name, _schema, **_kwargs):
        captured.update(instructions=instructions, data=data)
        return {"result": {"recommendation": "hold", "confidence": 0.9,
                            "reason": "No affirmative takeover candidate.",
                            "visual": {"person_visibility": "visible", "board_visibility": "not_visible",
                                       "activity": "seated", "summary": "A seated person is visible."}},
                "response_id": "camera-resp", "latency_ms": 7, "input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(agent_app, "_camera_image_for_round", fake_image)
    monkeypatch.setattr(agent_app, "_infer", fake_infer)
    monkeypatch.setattr(agent_app, "_http_json", lambda *_a, **_k: {"ok": True})
    agent_app._camera_job(
        {"agent_id": "camera-closeup2", "role": "camera", "camera_id": "closeup2",
         "controller_url": agent_app.DEFAULT_CONTROLLER_URL}, {"round": round_data},
    )

    prompt = captured["instructions"].lower()
    assert "current measured audio is the primary evidence" in prompt
    assert "cannot establish movement over time" in prompt
    assert "hold means no affirmative takeover recommendation" in prompt
    assert "clear face, posture, headset, or static board alone is not a reason" in prompt
    assert captured["data"]["camera_signal"]["speaking"] is True


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (TimeoutError("provider response body must not be logged"), "agent_timeouterror"),
        (agent_app.AgentTaskError("camera_image_timestamp_invalid"), "camera_image_timestamp_invalid"),
    ],
)
def test_failed_worker_job_releases_lease_once_and_does_not_retry_same_request(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected_code: str,
):
    lease = _round()
    posts: list[dict] = []
    job_calls: list[str] = []
    waits: list[float] = []

    class StopAfterTwoPolls:
        def __init__(self):
            self.count = 0

        def is_set(self):
            return self.count >= 2

        def wait(self, seconds):
            waits.append(seconds)
            self.count += 1
            return self.is_set()

    def fake_http(_url, path, *, payload=None, **_kwargs):
        if path.endswith("/camera-closeup2"):
            return lease
        posts.append(payload)
        return {"ok": True, "released": True, "duplicate": False}

    def failing_job(_config, _task):
        job_calls.append("called")
        raise failure

    monkeypatch.setattr(agent_app, "_http_json", fake_http)
    monkeypatch.setattr(agent_app, "_camera_job", failing_job)
    config = {"agent_id": "camera-closeup2", "role": "camera", "camera_id": "closeup2",
              "controller_url": agent_app.DEFAULT_CONTROLLER_URL}
    agent_app._continuous_role_loop(object(), config, StopAfterTwoPolls(), None)

    assert job_calls == ["called"]
    assert len(posts) == 1
    assert posts[0]["error_code"] == expected_code
    assert posts[0]["agent_id"] == "camera-closeup2"
    assert posts[0]["request_id"] == lease["request_id"]
    assert posts[0]["session_id"] == lease["session_id"]
    assert posts[0]["epoch"] == lease["epoch"]
    assert posts[0]["override_epoch"] == lease["override_epoch"]
    assert posts[0]["model_epoch"] == lease["model_epoch"]
    assert 0 <= posts[0]["elapsed_ms"] <= 30_000
    assert all(0 < duration < 1 for duration in waits)


def test_expired_deadline_posts_failure_and_polls_again_without_running_model(monkeypatch: pytest.MonkeyPatch):
    lease = _round()
    lease["deadline_remaining_ms"] = 300
    posts: list[dict] = []
    job_calls: list[str] = []

    class StopAfterOnePoll:
        def __init__(self):
            self.done = False

        def is_set(self):
            return self.done

        def wait(self, _seconds):
            self.done = True
            return True

    def fake_http(_url, path, *, payload=None, **_kwargs):
        if path.endswith("/camera-closeup2"):
            return lease
        posts.append(payload)
        return {"ok": True, "released": True, "duplicate": False}

    monkeypatch.setattr(agent_app, "_http_json", fake_http)
    monkeypatch.setattr(agent_app, "_camera_job", lambda *_a, **_k: job_calls.append("called"))
    config = {"agent_id": "camera-closeup2", "role": "camera", "camera_id": "closeup2",
              "controller_url": agent_app.DEFAULT_CONTROLLER_URL}
    agent_app._continuous_role_loop(object(), config, StopAfterOnePoll(), None)

    assert job_calls == []
    assert posts[0]["error_code"] == "deadline_insufficient"
    assert posts[0]["elapsed_ms"] == 0


def test_late_director_lease_is_released_without_starting_inference(monkeypatch: pytest.MonkeyPatch):
    lease = _round()
    lease["deadline_remaining_ms"] = 4_000
    posts: list[dict] = []
    model_calls: list[str] = []

    class FakeClock:
        now = 100.0

        def monotonic(self):
            return self.now

    class StopAfterOnePoll:
        def __init__(self):
            self.done = False

        def is_set(self):
            return self.done

        def wait(self, _seconds):
            self.done = True
            return True

    clock = FakeClock()

    def fake_http(_url, path, *, payload=None, **_kwargs):
        if path.endswith("/director"):
            # The request spent 2.1 seconds in transit, leaving under the
            # required 3.5-second director inference floor.
            clock.now += 2.1
            return lease
        posts.append(payload)
        return {"ok": True, "released": True, "duplicate": False}

    monkeypatch.setattr(agent_app, "time", clock)
    monkeypatch.setattr(agent_app, "_http_json", fake_http)
    monkeypatch.setattr(agent_app, "_director_job", lambda *_a, **_k: model_calls.append("called"))
    config = {"agent_id": "director", "role": "director",
              "controller_url": agent_app.DEFAULT_CONTROLLER_URL}
    agent_app._continuous_role_loop(object(), config, StopAfterOnePoll(), None)

    assert model_calls == []
    assert len(posts) == 1
    assert posts[0]["error_code"] == "deadline_insufficient"
    assert posts[0]["agent_id"] == "director"

