import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mix_field_recordings as mix


class FieldRecordingMixTests(unittest.TestCase):
    def source(self, **overrides):
        return {'sound_id': 'a', 'license': 'CC-BY-3.0', 'commercial_use_verified': True,
                'field_recording_verified_from_creator_description': True, 'channels': 2,
                'duration_seconds': 5000, 'sha256': 'a' * 64, 'author': 'Fixture recordist',
                'source_page': 'https://example.test/source',
                'license_url': 'https://creativecommons.org/licenses/by/3.0/',
                'local_path': 'unused.flac', **overrides}

    def test_cc_by_three_is_accepted_and_noncommercial_is_rejected(self):
        plan = [{'sound_id': 'a', 'start': 0, 'duration': 100}]
        self.assertEqual(mix.validate_plan({'a': self.source()}, plan, 16)[0]['license'], 'CC-BY-3.0')
        with self.assertRaises(mix.MixError):
            mix.validate_plan({'a': self.source(license='CC-BY-NC-3.0')}, plan, 16)

    def test_repeated_intervals_are_rejected(self):
        with self.assertRaisesRegex(mix.MixError, 'repeats'):
            mix.validate_plan({'a': self.source()}, [{'sound_id': 'a', 'start': 0, 'duration': 100},
                                                     {'sound_id': 'a', 'start': 90, 'duration': 100}], 16)

    def test_quiet_shore_plan_is_one_hour_and_avoids_flagged_events(self):
        root = Path(__file__).resolve().parents[1]
        plan = json.loads((root / 'config/quiet-shore-audio-plan.json').read_text())
        sources = {e['sound_id']: self.source(sha256=e['sound_id']) for e in plan['edits']}
        edits = mix.validate_plan(sources, plan['edits'], plan['crossfade_seconds'])
        self.assertEqual(edits[-1]['timeline_end'], 3600)
        flagged = {'aporee_37802_43261': [1365, 1659, 1701, 1713, 1751, 1921, 2004],
                   'aporee_41944_47808': [8, 1687], 'aporee_59218_67955': [524]}
        for edit in edits:
            self.assertFalse(any(edit['source_start'] <= t <= edit['source_end']
                                 for t in flagged[edit['sound_id']]))

    def test_short_full_pipeline_preserves_stereo_and_reuses_verified_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = []
            for index, frequency in enumerate([440, 660]):
                path = root / f'{index}.wav'
                mix.ffmpeg(['-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration=3:sample_rate=48000',
                            '-ac', '2', '-c:a', 'pcm_f32le', str(path)])
                sources.append(self.source(sound_id=str(index), local_path=str(path),
                                           duration_seconds=3, sha256=mix.sha256(path)))
            manifest = root / 'sources.json'; manifest.write_text(json.dumps({'sources': sources}))
            plan = root / 'plan.json'; plan.write_text(json.dumps({'theme': 'test-fixtures', 'duration_seconds': 5,
                'crossfade_seconds': 1, 'edits': [{'sound_id': str(i), 'start': 0, 'duration': 3} for i in range(2)]}))
            output = root / 'mix.flac'
            report = mix.make_mix(manifest, output, plan)
            self.assertTrue(report['full_decode_passed'])
            self.assertEqual(report['duration_seconds'], 5)
            self.assertEqual(mix.probe(output)['streams'][0]['channels'], 2)
            with patch.object(mix, 'ffmpeg', side_effect=AssertionError('Verified retry must not re-encode')):
                self.assertEqual(mix.make_mix(manifest, output, plan)['sha256'], report['sha256'])
            output.write_bytes(b'Existing file must be protected')
            with self.assertRaisesRegex(mix.MixError, 'Existing output'):
                mix.make_mix(manifest, output, plan)


if __name__ == '__main__':
    unittest.main()
