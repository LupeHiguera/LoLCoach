"""Set up a separate OBS profile/scene and optionally record only synthetic colour bars."""
import argparse
import json
import os
import time
from pathlib import Path

from coach.clips import run_media, video_duration
from fetch_matches import load_dotenv
from recorder import ObsClient, ObsError, wait_for_recording

PROFILE = 'LoLCoach'
SCENE = 'LoLCoach game'
WINDOW_INPUT = 'LoLCoach game window'
TEST_SCENE = 'LoLCoach capture test'
TEST_INPUT = 'LoLCoach test colour'


def scene_input(obs, scene, name, kind, settings):
    """Create only our named scene/source; preserve every existing user scene."""
    scenes = obs.request('GetSceneList')['scenes']
    if not any(s['sceneName'] == scene for s in scenes):
        obs.request('CreateScene', dict(sceneName=scene))
    inputs = obs.request('GetInputList')['inputs']
    existing = next((i for i in inputs if i['inputName'] == name), None)
    if existing and existing['inputKind'] != kind:
        raise ValueError(f'{name} already exists with a different source type')
    if existing:
        obs.request('SetInputSettings', dict(inputName=name, inputSettings=settings, overlay=True))
    else:
        obs.request('CreateInput', dict(sceneName=scene, inputName=name, inputKind=kind,
                                       inputSettings=settings, sceneItemEnabled=True))
    items = obs.request('GetSceneItemList', dict(sceneName=scene))['sceneItems']
    if any(item['sourceName'] != name for item in items):
        raise ValueError(f'{scene} contains additional sources; inspect them before recording')


def configure(obs, directory):
    """Dedicated MKV/NVENC recording profile; retain native video dimensions and FPS."""
    if obs.request('GetRecordStatus')['outputActive'] or obs.request('GetStreamStatus')['outputActive']:
        raise ValueError('Stop the existing recording/stream before configuring capture')
    profiles = obs.request('GetProfileList')['profiles']
    original_video = obs.request('GetVideoSettings')
    if PROFILE not in profiles:
        obs.request('CreateProfile', dict(profileName=PROFILE))
        # Profile creation is queued by OBS; wait for its configuration to appear.
        deadline = time.monotonic() + 3
        while PROFILE not in obs.request('GetProfileList')['profiles']:
            if time.monotonic() >= deadline:
                raise ValueError('OBS has not finished creating the LoLCoach profile')
            time.sleep(0.05)
    if obs.request('GetProfileList')['currentProfileName'] != PROFILE:
        obs.request('SetCurrentProfile', dict(profileName=PROFILE))
    for category, name, value in (
            ('Output', 'Mode', 'Simple'), ('SimpleOutput', 'RecFormat2', 'mkv'),
            ('SimpleOutput', 'RecEncoder', 'nvenc'), ('SimpleOutput', 'RecQuality', 'HQ')):
        obs.request('SetProfileParameter', dict(parameterCategory=category,
                                              parameterName=name, parameterValue=value))
    directory.mkdir(parents=True, exist_ok=True)
    obs.request('SetRecordDirectory', dict(recordDirectory=str(directory.resolve())))
    # SetProfileParameter persists settings; reload the profile to refresh encoders.
    other = next((p for p in obs.request('GetProfileList')['profiles'] if p != PROFILE), None)
    if other:
        obs.request('SetCurrentProfile', dict(profileName=other))
        time.sleep(0.5)
        obs.request('SetCurrentProfile', dict(profileName=PROFILE))
        time.sleep(0.5)
    obs.request('SetVideoSettings', original_video)
    scene_input(obs, SCENE, WINDOW_INPUT, 'window_capture', dict(
        window='League of Legends (TM) Client:RiotWindowClass:League of Legends.exe',
        priority=2, method=2, client_area=True, cursor=True, capture_audio=False))
    windows = obs.request('GetInputPropertiesListPropertyItems', dict(
        inputName=WINDOW_INPUT, propertyName='window'))['propertyItems']
    matches = [w for w in windows if w.get('itemEnabled') and
               str(w.get('itemValue', '')).lower().endswith(':league of legends.exe')]
    if matches:
        obs.request('SetInputSettings', dict(inputName=WINDOW_INPUT,
                    inputSettings=dict(window=matches[0]['itemValue']), overlay=True))
    obs.request('SetCurrentProgramScene', dict(sceneName=SCENE))
    video = obs.request('GetVideoSettings')
    print(f"LoLCoach profile: MKV, NVENC H.264, {video['outputWidth']}x{video['outputHeight']}, "
          f"{video['fpsNumerator'] / video['fpsDenominator']:g} fps.")
    print('Game window available.' if matches else 'Game window not running; capture still needs a Practice Tool check.')
    return video


def test_recording(obs, video, ffprobe):
    """Record colours only, with audio muted; exercise real OBS start/stop and decoding."""
    scene_input(obs, TEST_SCENE, TEST_INPUT, 'color_source_v3', dict(
        color=0xFF0000FF, width=video['baseWidth'], height=video['baseHeight']))
    muted = []
    started = False
    try:
        for item in obs.request('GetInputList')['inputs']:
            if item['inputKind'] in ('wasapi_input_capture', 'wasapi_output_capture'):
                name = item['inputName']
                state = obs.request('GetInputMute', dict(inputName=name))['inputMuted']
                muted.append((name, state))
                obs.request('SetInputMute', dict(inputName=name, inputMuted=True))
        obs.request('SetCurrentProgramScene', dict(sceneName=TEST_SCENE))
        time.sleep(0.5)
        initial_stats = obs.request('GetStats')
        obs.request('StartRecord')
        started = True
        wait_for_recording(obs)
        time.sleep(2)
        obs.request('SetInputSettings', dict(inputName=TEST_INPUT,
                    inputSettings=dict(color=0xFF00FF00), overlay=True))
        time.sleep(2)
        final_stats = obs.request('GetStats')
        result = obs.request('StopRecord')
        started = False
    finally:
        try:
            if started and obs.request('GetRecordStatus')['outputActive']:
                obs.request('StopRecord')
        finally:
            obs.request('SetCurrentProgramScene', dict(sceneName=SCENE))
            for name, state in muted:
                obs.request('SetInputMute', dict(inputName=name, inputMuted=state))
    path = Path(result['outputPath'])
    duration = video_duration(path, ffprobe)
    streams = json.loads(run_media([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                                  '-show_entries', 'stream=codec_name,width,height', '-of', 'json', str(path)]))
    print(json.dumps(dict(synthetic=True, duration_s=duration, video=streams['streams'],
                          skipped_frames=final_stats['outputSkippedFrames'] - initial_stats['outputSkippedFrames'],
                          total_frames=final_stats['outputTotalFrames'] - initial_stats['outputTotalFrames']), indent=2))
    print(f'Test recording saved locally: {path}')
    return path


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='Record 4 seconds of generated colour, never the desktop')
    parser.add_argument('--ffprobe', default='ffprobe')
    args = parser.parse_args(argv)
    if not os.environ.get('OBS_WS_PASSWORD'):
        parser.exit(1, 'Configure OBS_WS_PASSWORD in .env first.\n')
    try:
        with ObsClient(os.environ['OBS_WS_PASSWORD']) as obs:
            video = configure(obs, root / 'data' / 'recordings')
            if args.test:
                test_recording(obs, video, args.ffprobe)
    except (ObsError, OSError, ValueError) as exc:
        parser.exit(1, f'Capture setup failed: {exc}\n')


if __name__ == '__main__':
    main()
