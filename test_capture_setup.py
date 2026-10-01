"""Capture setup must preserve active outputs and restore the scene/audio after errors."""
import unittest
from unittest import mock

from coach import capture_setup
from recorder import ObsError


class CaptureSafetyTests(unittest.TestCase):
    def test_active_recording_is_never_reconfigured(self):
        obs = mock.Mock()
        obs.request.return_value = {'outputActive': True}
        with self.assertRaisesRegex(ValueError, 'Stop the existing'):
            capture_setup.configure(obs, mock.Mock())
        self.assertEqual(obs.request.call_args_list, [mock.call('GetRecordStatus')])

    def test_existing_test_scene_cannot_capture_other_sources(self):
        obs = mock.Mock()
        answers = {
            'GetSceneList': {'scenes': [{'sceneName': capture_setup.TEST_SCENE}]},
            'GetInputList': {'inputs': [{'inputName': capture_setup.TEST_INPUT, 'inputKind': 'color_source_v3'}]},
            'GetSceneItemList': {'sceneItems': [{'sourceName': 'Desktop capture'}]},
        }
        obs.request.side_effect = lambda name, *args: answers.get(name, {})
        with self.assertRaisesRegex(ValueError, 'additional sources'):
            capture_setup.scene_input(obs, capture_setup.TEST_SCENE, capture_setup.TEST_INPUT,
                                      'color_source_v3', {})
        self.assertFalse(any(c.args[0] == 'StartRecord' for c in obs.request.call_args_list))

    def test_failed_start_restores_game_scene_and_audio(self):
        obs = mock.Mock()
        answers = {
            'GetInputList': {'inputs': [{'inputName': 'Microphone', 'inputKind': 'wasapi_input_capture'}]},
            'GetInputMute': {'inputMuted': False},
            'GetRecordStatus': {'outputActive': False},
        }
        obs.request.side_effect = lambda name, *args: answers.get(name, {})
        with mock.patch.object(capture_setup, 'scene_input'), mock.patch.object(capture_setup.time, 'sleep'), \
                mock.patch.object(capture_setup, 'wait_for_recording', side_effect=ObsError('inactive')):
            with self.assertRaisesRegex(ObsError, 'inactive'):
                capture_setup.test_recording(obs, {'baseWidth': 3840, 'baseHeight': 2160}, 'ffprobe')
        obs.request.assert_any_call('SetCurrentProgramScene', {'sceneName': capture_setup.SCENE})
        obs.request.assert_any_call('SetInputMute', {'inputName': 'Microphone', 'inputMuted': False})


if __name__ == '__main__':
    unittest.main()
