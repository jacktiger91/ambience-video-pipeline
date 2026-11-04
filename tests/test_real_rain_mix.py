import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import array

import mix_real_rain as mix


class RealRainMixTests(unittest.TestCase):
    def source(self, **overrides):
        return {'sound_id':'a', 'license':'CC0-1.0','commercial_use_verified':True,
                'field_recording_verified_from_creator_description':True,'channels':2,
                'duration_seconds':5000,'sha256':'a'*64,'author':'Test recordist',
                'source_page':'https://example.test/source','license_url':'https://creativecommons.org/publicdomain/zero/1.0/',
                'local_path':'unused.flac',**overrides}

    def test_unverified_noncommercial_and_mono_sources_rejected(self):
        for changed in ({'commercial_use_verified':False},{'license':'CC-BY-NC-4.0'},{'channels':1},{'field_recording_verified_from_creator_description':False}):
            with self.subTest(changed=changed), self.assertRaises(mix.MixError):
                mix.validate_plan({'a':self.source(**changed)},[{'sound_id':'a','start':0,'duration':100}],16)

    def test_overlapping_source_intervals_rejected(self):
        with self.assertRaisesRegex(mix.MixError,'repeats'):
            mix.validate_plan({'a':self.source()},[{'sound_id':'a','start':0,'duration':100},{'sound_id':'a','start':90,'duration':100}],16)

    def test_production_plan_is_one_hour_without_repeated_sources(self):
        sources={e['sound_id']:self.source(sha256=str(i)*64) for i,e in enumerate(mix.PLAN)}
        edits=mix.validate_plan(sources,mix.PLAN,mix.CROSSFADE)
        self.assertEqual(edits[-1]['timeline_end'],3600)
        self.assertEqual([e['timeline_start'] for e in edits],[0,314,1548,2682])

    def test_real_ffmpeg_crossfade_preserves_stereo_and_expected_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            paths=[]
            for i,f in enumerate((440,660)):
                path=root/f'{i}.wav'
                mix.ffmpeg(['-f','lavfi','-i',f'sine=frequency={f}:sample_rate=48000:duration=3',
                            '-ac','2','-c:a','pcm_s16le',str(path)])
                paths.append(path)
            edits=[{'path':str(p),'source_start':0,'duration':3,'timeline_end':3 if i==0 else 5} for i,p in enumerate(paths)]
            output=root/'mix.flac'
            mix.mix_segments(edits,[0,0],1,output)
            data=mix.probe(output)
            self.assertEqual(data['streams'][0]['channels'],2)
            self.assertAlmostEqual(float(data['format']['duration']),5,places=3)
            mix.ffmpeg(['-v','error','-xerror','-i',str(output),'-f','null','-'])

    def test_float_intermediate_preserves_hot_thunder_headroom(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'source.wav'
            mix.ffmpeg(['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3',
                        '-ac','2','-c:a','pcm_f32le',str(source)])
            output=root/'assembled.wav'
            mix.mix_segments([{'path':str(source),'source_start':0,'duration':3,'timeline_end':3}],[26],0,output)
            decoded=subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','f32le','-'],capture_output=True,check=True)
            samples=array.array('f');samples.frombytes(decoded.stdout)
            self.assertGreater(max(abs(sample) for sample in samples),1.5)
            self.assertEqual(mix.probe(output)['streams'][0]['codec_name'],'pcm_f32le')

    def test_ebu_r128_agrees_with_loudnorm_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.wav'
            mix.ffmpeg(['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5',
                        '-ac','2','-c:a','pcm_f32le',str(source)])
            reference=mix.loudness(source)
            measured=mix.measure_levels(source)
            self.assertAlmostEqual(float(reference['input_i']),float(measured['input_i']),delta=0.2)
            self.assertAlmostEqual(float(reference['input_tp']),float(measured['input_tp']),delta=0.2)


if __name__=='__main__':
    unittest.main()
