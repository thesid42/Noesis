from __future__ import annotations

import copy
import io
import json

import pytest
from PIL import Image

from noesis.controller import ControllerError
from test_ai_api import ai_result, make_ai_controller


def install_image_source(controller, media):
    output = io.BytesIO()
    Image.new('RGB', (64, 36), 'blue').save(output, format='JPEG')
    jpeg = output.getvalue()
    controller._latest_media['broadcast'] = {'buffer_epoch': 7}
    calls = []

    def getter(camera_id, time_s, *, expected_buffer_epoch=None):
        calls.append((camera_id, time_s, expected_buffer_epoch))
        return {'camera_id': camera_id, 'frame_time_s': time_s, 'source_time_s': time_s,
                'buffer_epoch': 7, 'jpeg': jpeg}

    media.agent_image_at = getter
    return calls


def visual_result(lease):
    return ai_result(
        lease, role='camera', camera_id=lease['camera_id'],
        image={k: v for k, v in lease['camera_image'].items() if k != 'image_url'},
        result={'recommendation': 'avoid', 'confidence': .95, 'reason': 'The image shows an empty seat.',
                'visual': {'person_visibility': 'absent', 'board_visibility': 'not_visible',
                           'activity': 'empty', 'summary': 'Empty seat.'}},
    )


@pytest.mark.asyncio
async def test_camera_image_is_immutable_pinned_and_not_in_state_or_critic_lease():
    controller, media, _ = await make_ai_controller()
    calls = install_image_source(controller, media)
    lease = await controller.ai_lease('camera-closeup3')
    assert calls == [('closeup3', lease['media_time_s'], 7)]
    assert lease['camera_image']['image_url'].startswith('data:image/jpeg;base64,')
    media.agent_image_at = lambda *args, **kwargs: None
    assert (await controller.ai_lease('camera-closeup3'))['camera_image'] == lease['camera_image']
    assert 'data:image' not in json.dumps(await controller.get_state())
    await controller.accept_ai_report(visual_result(lease))
    state = await controller.get_state()
    report = state['flower']['inference_results'][0]
    assert report['image']['sha256'] == lease['camera_image']['sha256']
    assert report['result']['visual']['activity'] == 'empty'
    assert 'data:image' not in json.dumps(state)
    critic = await controller.ai_lease('critic')
    assert critic['previous_reports'][0]['image'] == report['image']
    assert 'data:image' not in json.dumps(critic)


@pytest.mark.asyncio
async def test_forged_missing_or_cross_camera_image_evidence_is_rejected():
    controller, media, _ = await make_ai_controller()
    install_image_source(controller, media)
    lease = await controller.ai_lease('camera-closeup2')
    valid = visual_result(lease)
    for change in ({'sha256': '0' * 64}, {'camera_id': 'closeup1'}, {'frame_time_s': 999.0}):
        bad = copy.deepcopy(valid)
        bad['image'].update(change)
        with pytest.raises(ControllerError, match='image provenance'):
            await controller.accept_ai_report(bad)
    bad = copy.deepcopy(valid); bad.pop('image')
    with pytest.raises(ControllerError, match='image provenance'):
        await controller.accept_ai_report(bad)
    bad = copy.deepcopy(valid); bad['result'].pop('visual')
    with pytest.raises(ControllerError, match='requires a visual'):
        await controller.accept_ai_report(bad)
    await controller.accept_ai_report(valid)


@pytest.mark.asyncio
async def test_pause_invalidates_image_lease_and_returned_assessment():
    controller, media, _ = await make_ai_controller()
    install_image_source(controller, media)
    lease = await controller.ai_lease('camera-closeup1')
    await controller.session_pause()
    assert not controller._ai_role_leases
    with pytest.raises(ControllerError, match='inactive'):
        await controller.accept_ai_report(visual_result(lease))


@pytest.mark.asyncio
async def test_missing_image_cannot_claim_visual_observation():
    controller, _, _ = await make_ai_controller()
    lease = await controller.ai_lease('camera-closeup1')
    assert lease['camera_image'] is None
    body = ai_result(lease, role='camera', camera_id='closeup1')
    body['result']['visual'] = {'person_visibility': 'absent', 'board_visibility': 'uncertain',
                                'activity': 'uncertain', 'summary': 'Image unavailable.'}
    with pytest.raises(ControllerError, match='Visual claims'):
        await controller.accept_ai_report(body)
    body['result']['visual']['person_visibility'] = 'uncertain'
    await controller.accept_ai_report(body)


@pytest.mark.asyncio
async def test_legacy_round_pins_images_without_exposing_them_in_state():
    controller, media, _ = await make_ai_controller()
    install_image_source(controller, media)
    round_data = await controller.ai_round()
    assert len(round_data['camera_images']) == 4
    assert all(image['camera_id'] == camera for camera, image in round_data['camera_images'].items())
    assert 'data:image' not in json.dumps(await controller.get_state())
