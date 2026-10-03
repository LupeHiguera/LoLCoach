import copy
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from coach import harness, observer
from coach.clips import plan_clips, run_media


def inputs(count=8):
    manifest = plan_clips(dict(match_id='test-match', patch='test', my_champion='Ahri',
                               opp_champion='Yone', duration_s=4),
                          [dict(time=1000, me='death')], 0, 4, 1, 1)
    packet, clip = harness.make_packet(manifest, 'death-1000', 900, 700, count)
    views = dict(schema_version=1, moment_id=packet['moment_id'], packet_sha256=observer.fingerprint(packet),
                 source_size=[64, 48], crops=[dict(frame_id=f['id'], game_ms=f['game_ms'], region=r,
                                                  image_file=f"{r}-{f['game_ms']}.png", rect=[0, 0, 16, 16])
                                             for f in packet['frames'] for r in observer.REGIONS])
    return manifest, packet, clip, views


def png(width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * width) * height)) + chunk(b'IEND', b''))


class ViewTests(unittest.TestCase):
    def test_eight_instants_have_24_views_but_only_eight_citation_ids(self):
        _, packet, _, views = inputs()
        payload = observer.build_request(packet, views, None, 'test-local', preview=True, crop_frames='all')
        content = payload['messages'][1]['content']
        self.assertEqual(sum(c['type'] == 'image_url' for c in content), 24)
        context = json.loads(content[0]['text'])['packet']
        self.assertEqual(len(context['frames']), 8)
        self.assertEqual(context['events'], [])
        self.assertIsNone(context.get('state'))
        self.assertNotIn('image_file', content[0]['text'])
        self.assertNotIn('data:image', json.dumps(payload))
        self.assertEqual([m['role'] for m in payload['messages']], ['system', 'user'])

    def test_default_keeps_eight_whole_frames_and_only_final_crops(self):
        _, packet, _, views = inputs()
        payload = observer.build_request(packet, views, None, 'test-local', preview=True)
        content = payload['messages'][1]['content']
        self.assertEqual(sum(c['type'] == 'image_url' for c in content), 10)
        crop_labels = [c['text'] for c in content if c['type'] == 'text' and 'no resizing' in c['text']]
        self.assertEqual(len(crop_labels), 2)
        self.assertTrue(all('game 900 ms' in label for label in crop_labels))

    def test_crops_cannot_add_future_times_paths_or_unbounded_pixels(self):
        _, packet, _, original = inputs()
        for change in ('future', 'path', 'outside', 'huge', 'duplicate', 'packet'):
            views = copy.deepcopy(original)
            crop = views['crops'][0]
            if change == 'future': crop['game_ms'] = 1001
            elif change == 'path': crop['image_file'] = '../secret.png'
            elif change == 'outside': crop['rect'] = [60, 0, 16, 16]
            elif change == 'huge':
                views['source_size'] = [4000, 4000]
                crop['rect'] = [0, 0, 2000, 2000]
            elif change == 'duplicate': views['crops'][1] = dict(crop)
            else: views['packet_sha256'] = 'wrong'
            with self.subTest(change=change), self.assertRaises(ValueError):
                observer.validate_views(views, packet)

    def test_native_dimensions_and_aggregate_bytes_are_enforced(self):
        _, packet, _, views = inputs(1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for frame in packet['frames']:
                (root / frame['image_file']).write_bytes(png(32, 24))
            for crop in views['crops']:
                (root / crop['image_file']).write_bytes(png(16, 16))
            observer.build_request(packet, views, root, 'test-local')
            with mock.patch.object(observer, 'MAX_TOTAL_IMAGE_BYTES', 1), self.assertRaisesRegex(ValueError, 'total byte'):
                observer.build_request(packet, views, root, 'test-local')
            (root / views['crops'][0]['image_file']).write_bytes(png(8, 8))
            with self.assertRaisesRegex(ValueError, 'dimensions'):
                observer.build_request(packet, views, root, 'test-local')

    def test_packet_edits_invalidate_crops_including_timeline_edits(self):
        _, packet, _, views = inputs()
        packet['focus'] = 'different focus'
        with self.assertRaisesRegex(ValueError, 'match'):
            observer.validate_views(views, packet)

    def test_check_sheet_never_marks_model_claims_verified(self):
        _, packet, _, views = inputs(1)
        f = packet['frames'][0]
        obs = dict(schema_version=1, moment_id=packet['moment_id'], observations=[dict(
            id='obs-1', game_ms=f['game_ms'], statement='A shape is visible.', source='local_vision',
            verification='model_observed', evidence_refs=[f['id']])], unknowns=[])
        sheet = observer.check_sheet(packet, views, obs)
        self.assertEqual(sheet['checks'][0]['verdict'], 'pending')
        self.assertEqual(len(sheet['checks'][0]['evidence'][0]['crops']), 2)
        self.assertEqual(obs['observations'][0]['verification'], 'model_observed')

    def test_failure_does_not_publish_partial_bundle_or_edit_source(self):
        manifest, packet, clip, _ = inputs(2)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'manifest.json').write_text(json.dumps(manifest))
            (root / 'packet.json').write_text(json.dumps(packet))
            (root / clip['file']).write_bytes(b'fixture')
            for f in packet['frames']:
                (root / f['image_file']).write_bytes(png(32, 24))
            before = (root / 'packet.json').read_bytes()
            with mock.patch.object(observer, 'run_media', side_effect=[json.dumps(dict(streams=[dict(width=64,height=48)])), ValueError('decode')]):
                with self.assertRaisesRegex(ValueError, 'decode'):
                    observer.prepare(root/'manifest.json', root/'packet.json', root/'out', dict(hud=[0,0,16,16],minimap=[32,32,16,16]))
            self.assertFalse((root/'out').exists())
            self.assertFalse(list(root.glob('.views-*')))
            self.assertEqual((root/'packet.json').read_bytes(), before)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
    def test_real_media_native_crops_and_original_packet_preserved(self):
        manifest, packet, clip, _ = inputs(2)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'manifest.json').write_text(json.dumps(manifest))
            run_media(['ffmpeg','-v','error','-f','lavfi','-i','testsrc=size=64x48:rate=10','-t','2',
                       '-pix_fmt','yuv420p',str(root/clip['file'])])
            harness.prepare_bundle(root/'manifest.json', packet, clip, root/'base')
            observer.prepare(root/'manifest.json', root/'base/packet.json', root/'views',
                             dict(hud=[16,16,16,16],minimap=[32,32,16,16]))
            self.assertEqual(json.loads((root/'views/packet.json').read_text()), packet)
            views = json.loads((root/'views/views.json').read_text())
            payload = observer.build_request(packet, views, root/'views', 'test-local')
            self.assertEqual(sum(c['type']=='image_url' for c in payload['messages'][1]['content']), 4)


if __name__ == '__main__':
    unittest.main()
