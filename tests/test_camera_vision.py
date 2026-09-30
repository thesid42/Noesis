from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flower_apps"))
from noesis_agents import agent_app
from noesis_agents.policy import parse_camera_result, safe_round_input


def _jpeg(color: tuple[int, int, int] = (30, 90, 160), size: tuple[int, int] = (64, 48)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="JPEG", quality=80)
    return stream.getvalue()


def _image(camera_id: str = "closeup1", *, source_time_s: float = 10.0,
           frame_time_s: float = 9.9, image_bytes: bytes | None = None) -> dict:
    payload = image_bytes if image_bytes is not None else _jpeg()
    return {
        "camera_id": camera_id,
        "frame_time_s": frame_time_s,
        "source_time_s": source_time_s,
        "buffer_epoch": 4,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "width": 64,
        "height": 48,
        "image_url": "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"),
    }


def _round(image: dict | None = None) -> dict:
    value = {
        "request_id": "request-1", "session_id": "session-1", "epoch": 2,
        "override_epoch": 1, "model_epoch": 3, "observation_revision": 9,
        "media_time_s": 12.0, "model": "qwen/qwen3.5-9b", "profile": "flower_camera",
        "cameras": [
            {"id": "closeup1", "healthy": True, "status": "ready", "speaker_state": "quiet", "age_ms": 20},
            {"id": "closeup2", "healthy": True, "status": "ready", "speaker_state": "speaking", "age_ms": 22},
        ],
        "program": {"camera_id": "closeup2", "reason": "Prior editorial text must not bias a camera observer."},
        "recent_events": [{"kind": "old_claim", "source": "model"}],
        "previous_reports": [{"role": "camera", "result": {"reason": "earlier claim"}}],
        "editorial_context": {"shot_history": [{"camera_id": "closeup1", "reason": "earlier claim"}]},
    }
    if image is not None:
        value["camera_image"] = image
    return value


def _camera_config(camera_id: str = "closeup1") -> dict:
    return {"agent_id": f"camera-{camera_id}", "role": "camera", "camera_id": camera_id,
            "controller_url": agent_app.DEFAULT_CONTROLLER_URL}


def test_image_is_sent_as_exactly_one_low_detail_input_image_outside_text_json():
    image = _image()
    payload = base64.b64decode(image["image_url"].split(",", 1)[1])
    args = agent_app._model_request_args(
        "qwen/qwen3.5-9b", "instructions", {"camera": "closeup1"}, "camera", {"type": "object"},
        reasoning_effort="none", image_url=image["image_url"],
    )
    assert args["reasoning"] == {"effort": "none"}
    assert isinstance(args["input"], list) and len(args["input"]) == 1
    content = args["input"][0]["content"]
    assert [part["type"] for part in content] == ["input_text", "input_image"]
    assert content[1] == {"type": "input_image", "image_url": image["image_url"], "detail": "low"}
    assert base64.b64encode(payload).decode("ascii") not in content[0]["text"]


def test_camera_image_validation_checks_assignment_time_hash_size_and_dimensions():
    image = _image()
    metadata, url = agent_app._camera_image_for_round(_round(image), "closeup1")
    assert metadata == {key: value for key, value in image.items() if key != "image_url"}
    assert url == image["image_url"]

    with pytest.raises(agent_app.AgentTaskError, match="assignment"):
        agent_app._camera_image_for_round(_round(_image("closeup2")), "closeup1")
    with pytest.raises(agent_app.AgentTaskError, match="timestamp"):
        agent_app._camera_image_for_round(_round(_image(source_time_s=12.1)), "closeup1")
    corrupt = {**image, "sha256": "0" * 64}
    with pytest.raises(agent_app.AgentTaskError, match="bytes"):
        agent_app._camera_image_for_round(_round(corrupt), "closeup1")
    remote = {**image, "image_url": "https://example.invalid/camera.jpg"}
    with pytest.raises(agent_app.AgentTaskError, match="url"):
        agent_app._camera_image_for_round(_round(remote), "closeup1")
    oversized = _image(image_bytes=b"\xff\xd8" + b"x" * (128 * 1024) + b"\xff\xd9")
    with pytest.raises(agent_app.AgentTaskError, match="bytes"):
        agent_app._camera_image_for_round(_round(oversized), "closeup1")


def test_camera_job_uses_only_assigned_image_and_reports_non_base64_provenance(monkeypatch):
    image_one = _image("closeup1")
    image_two = _image("closeup2", image_bytes=_jpeg((170, 20, 60)))
    task_round = _round()
    task_round["camera_images"] = {"closeup1": image_one, "closeup2": image_two}
    seen: dict = {}
    posted: list[dict] = []

    def fake_infer(model, _instructions, data, _schema_name, _schema, **kwargs):
        seen.update(model=model, data=data, **kwargs)
        return {
            "result": {
                "recommendation": "take", "confidence": 0.8,
                "reason": "The assigned frame is clear.",
                "visual": {"person_visibility": "visible", "board_visibility": "visible",
                           "activity": "presenting", "summary": "A person and board are visible."},
            },
            "response_id": "vision-response-1", "latency_ms": 150,
            "input_tokens": 210, "output_tokens": 40,
        }

    monkeypatch.setattr(agent_app, "_infer", fake_infer)
    monkeypatch.setattr(agent_app, "_http_json", lambda _url, _path, *, payload=None, **_kwargs: posted.append(payload) or {"ok": True})
    result = agent_app._camera_job(_camera_config(), {"round": task_round})

    assert seen["model"] == "qwen/qwen3.5-9b"
    assert seen["image_url"] == image_one["image_url"]
    assert len(seen["data"]["round"]["cameras"]) == 1
    assert seen["data"]["round"]["cameras"][0]["id"] == "closeup1"
    assert "program" not in seen["data"]["round"]
    assert "recent_events" not in seen["data"]["round"]
    assert "previous_reports" not in seen["data"]["round"]
    assert "shot_history" not in seen["data"]["round"].get("editorial_context", {})
    assert posted[0]["image"] == {key: value for key, value in image_one.items() if key != "image_url"}
    assert "image_url" not in posted[0]["image"]
    assert result["report"]["result"]["visual"]["activity"] == "presenting"


def test_camera_schema_requires_visual_for_image_and_no_image_claims_are_normalized_uncertain():
    legacy = {"recommendation": "avoid", "confidence": 0.7, "reason": "The camera is unavailable."}
    no_image = parse_camera_result(legacy, has_image=False)
    assert no_image["visual"] == {
        "person_visibility": "uncertain", "board_visibility": "uncertain",
        "activity": "uncertain", "summary": "unavailable",
    }
    claimed = {**legacy, "visual": {"person_visibility": "visible", "board_visibility": "visible",
                                     "activity": "presenting", "summary": "invented"}}
    assert parse_camera_result(claimed, has_image=False)["visual"]["activity"] == "uncertain"
    with pytest.raises(ValueError, match="include visual"):
        parse_camera_result(legacy, has_image=True)
    parsed = parse_camera_result(claimed, has_image=True)
    assert parsed["visual"]["activity"] == "presenting"


def test_report_context_keeps_exact_image_provenance_without_jpeg_or_losing_visual_result():
    image = _image()
    report = {
        "role": "camera", "agent_id": "camera-closeup1", "camera_id": "closeup1",
        "response_id": "vision-accepted", "image": {key: value for key, value in image.items() if key != "image_url"},
        "result": {
            "recommendation": "take", "confidence": 0.9, "reason": "A person is presenting.",
            "visual": {"person_visibility": "visible", "board_visibility": "visible",
                       "activity": "presenting", "summary": "A person and board are visible."},
        },
    }
    safe = safe_round_input({"previous_reports": [report]})
    safe_report = safe["previous_reports"][0]
    assert safe_report["image"] == report["image"]
    assert "image_url" not in safe_report["image"]
    assert safe_report["result"]["visual"]["activity"] == "presenting"
    assert safe_report["image"]["frame_time_s"] == image["frame_time_s"]


def test_missing_causal_source_time_does_not_attach_an_image():
    image = _image()
    image["source_time_s"] = None
    assert agent_app._camera_image_for_round(_round(image), "closeup1") == (None, None)
