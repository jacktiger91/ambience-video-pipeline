"""Quality boundaries, date selection and durable retry behavior; no uploads."""
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

import daily_schedule
import prepare_bundle
import production
import production_quality as quality
import production_sources as sources
import production_store as storage
from scheduled_runner import RunnerError


class ProductionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def test_only_explicit_compatible_commercial_grants(self):
        self.assertEqual(sources.licence('https://creativecommons.org/licenses/by/4.0/')[0],'CC-BY-4.0')
        self.assertEqual(sources.licence('https://creativecommons.org/publicdomain/zero/1.0/')[0],'CC0-1.0')
        for url in ('https://creativecommons.org/licenses/by-nc/4.0/',
                    'https://creativecommons.org/licenses/by-sa/4.0/',
                    'https://creativecommons.org/licenses/by-nd/4.0/',
                    'https://creativecommons.org/publicdomain/mark/1.0/',
                    'https://example.invalid/free',None):
            with self.subTest(url=url),self.assertRaises(sources.SourceError):sources.licence(url)

    def test_event_margins_are_merged_without_reusing_audio(self):
        self.assertEqual(quality.clean_intervals(600,[120,125,400]),[(0.0,108.0),(138,250),(413,187)])
        self.assertEqual(quality.clean_intervals(80,[]),[])

    def test_exact_hour_uses_distinct_passages_and_refuses_short_supply(self):
        candidates=[{'sound_id':'a','start':0,'duration':1900},
                    {'sound_id':'b','start':50,'duration':1800}]
        edits=production.assemble_edits(candidates)
        self.assertEqual(sum(x['duration'] for x in edits)-16*(len(edits)-1),3600)
        self.assertEqual(edits[1]['duration'],1716)
        with self.assertRaisesRegex(production.ProductionError,'full hour'):
            production.assemble_edits(candidates[:1])
        with self.assertRaisesRegex(production.ProductionError,'overlap'):
            production.assemble_edits(candidates+[{'sound_id':'a','start':20,'duration':1800}])

    def test_motion_check_rejects_still_and_camera_pan(self):
        rng=np.random.default_rng(7)
        background=cv2.GaussianBlur(rng.integers(0,256,(180,320),dtype=np.uint8),(3,3),0)
        still=np.stack([background]*20)
        self.assertFalse(quality.motion_metrics(still)['subject_motion_visible'])
        pan=np.stack([np.roll(background,i*5,axis=1) for i in range(20)])
        self.assertFalse(quality.motion_metrics(pan)['camera_fixed'])
        # Independently moving foreground patches with a fixed majority background.
        frames=[]
        for i in range(20):
            frame=background.copy()
            frame[75:165,25:295]=np.roll(background[75:165,25:295],i*2,axis=1)
            frames.append(frame)
        result=quality.motion_metrics(np.stack(frames))
        self.assertTrue(result['camera_fixed'],result)
        self.assertTrue(result['subject_motion_visible'],result)

    def test_dates_continue_past_briefs_with_dst_deadline(self):
        config={'schema_version':1,'enabled':True,'timezone':'Asia/Nicosia',
                'attempt_from':'12:00','ready_by':'18:00','concept_catalog':'concepts.json',
                'automatic_production_start':'2026-11-06','prepared_episodes':{}}
        path=self.root/'config.json';path.write_text(json.dumps(config))
        (self.root/'concepts.json').write_text(json.dumps({'rotation_start_date':'2026-11-05','concepts':[{'id':'forest-dawn'}]}))
        for timestamp,identity,deadline in (
            ('2026-11-05T15:00:00+00:00','2026-11-05-forest-dawn','2026-11-05T16:00:00+00:00'),
            ('2027-07-01T10:00:00+00:00','2027-07-01-daily-ambience','2027-07-01T15:00:00+00:00')):
            result=daily_schedule.select_slot(path,datetime.fromisoformat(timestamp))
            self.assertEqual(result['bundle_id'],identity)
            self.assertEqual(result['ready_by_utc'],deadline)
            self.assertFalse(result['blockers'])

    def test_ready_entry_requires_all_three_quality_proofs(self):
        entry={'schema_version':1,'bundle_id':'2026-11-06-daily-ambience','asset_id':5,'asset_size':100,
               'asset_sha256':'a'*64,'delivery_identity':'b'*64,'full_decode_all_three':True,
               'source_permissions_verified':True,'actual_scene_motion_checked':True}
        with mock.patch.object(storage,'read_document',return_value=entry):
            self.assertEqual(storage.prepared_entry(mock.Mock(),entry['bundle_id']),entry)
            entry['actual_scene_motion_checked']=False
            with self.assertRaises(RunnerError):storage.prepared_entry(mock.Mock(),entry['bundle_id'])

    def test_checkpoint_restores_verified_bytes_and_portable_manifest(self):
        bundle_id='2026-11-06-daily-ambience';store=mock.Mock()
        store.assets.return_value=[{'name':'checkpoint-000001.json'}]
        body=b'verified unit fixture media';sha=hashlib.sha256(body).hexdigest()
        data={'schema_version':1,'bundle_id':bundle_id,'assets':{
            'long':{'filename':'long.mp4','asset_id':10,'sha256':sha,'size':len(body)}},
            'manifest':{'artifacts':{'long':{'path':'long.mp4','sha256':sha}}}}
        def download(asset,path,expected,size):
            self.assertEqual((expected,size),(sha,len(body)))
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        store.download.side_effect=download
        with mock.patch.object(storage,'read_document',return_value=data):
            checkpoint=storage.Checkpoints(store,self.root,bundle_id)
        path=checkpoint.restore()
        self.assertEqual(path.read_bytes(),body)
        manifest=json.loads((self.root/'review/bundle.json').read_text())
        self.assertEqual(manifest['artifacts']['long']['path'],str(path.resolve()))

    def test_interrupted_bundle_resumes_on_a_fresh_runner_without_recreating_long(self):
        store=mock.Mock();assets={};by_name={}
        def put_file(unused,name,path):
            body=path.read_bytes();sha=hashlib.sha256(body).hexdigest()
            if name in by_name:
                item=by_name[name];self.assertEqual(assets[item['asset_id']],body);return item
            item={'asset_id':len(assets)+1,'size':len(body),'sha256':sha,'name':name}
            assets[item['asset_id']]=body;by_name[name]=item;return item
        store.assets.side_effect=lambda:[{'id':v['asset_id'],'name':k,'size':v['size'],'state':'uploaded','digest':'sha256:'+v['sha256']} for k,v in by_name.items()]
        store.find_asset.side_effect=lambda identity:{'id':identity}
        def download(item,path,expected,size):
            body=assets[item['id']]
            self.assertEqual((hashlib.sha256(body).hexdigest(),len(body)),(expected,size))
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        store.download.side_effect=download
        first=self.root/'first';second=self.root/'second';first.mkdir();second.mkdir()
        for key in ('backdrop','thumbnail-source'):(first/(key+'.jpg')).write_bytes(key.encode())
        source=first/'source.mp4';source.write_bytes(b'unit fixture, no actual production-media claim')
        config={'bundle_id':'2026-11-06-daily-ambience','intended_channel_handle':'@QuietHoursRelaxingSounds',
                'theme':'Fixture','title':'Fixture hour','description':'Unit test only.','recording_credits':'Unit fixture.',
                'made_for_kids':False,'contains_synthetic_media':True,'long_video':str(source),
                'shorts':[{'title':'First fixture','description':'Unit test.','start':20,'duration':15,'framing':'left'},
                          {'title':'Second fixture','description':'Unit test.','start':80,'duration':15,'framing':'right'}]}
        episode=first/'episode.json';episode.write_text(json.dumps(config))
        def frame(source,path,offset,width,height):
            path.write_bytes(('frame-'+path.name).encode());return {'resolution':[width,height]}
        def short(source,path,config):
            path.write_bytes(('short-'+str(config['start'])).encode());return {'duration':config['duration'],'full_decode':True}
        with mock.patch.object(storage,'put_file',side_effect=put_file),mock.patch.object(prepare_bundle,'require_tools'), \
                mock.patch.object(prepare_bundle,'make_frame',side_effect=frame):
            checkpoint=storage.Checkpoints(store,first,config['bundle_id'])
            with mock.patch.object(prepare_bundle,'verify_media',return_value={'duration':3600,'full_decode':True}), \
                    mock.patch.object(prepare_bundle,'make_short',side_effect=RuntimeError('simulated interruption')):
                with self.assertRaisesRegex(RuntimeError,'interruption'):
                    prepare_bundle.prepare(episode,first/'review',on_artifact=checkpoint)
            recovered=storage.Checkpoints(store,second,config['bundle_id'])
            restored=recovered.restore();self.assertIsNotNone(restored)
            config['long_video']=str(restored);episode=second/'episode.json';episode.write_text(json.dumps(config))
            with mock.patch.object(prepare_bundle,'verify_media',side_effect=AssertionError('Long must be reused')), \
                    mock.patch.object(prepare_bundle,'make_short',side_effect=short) as shorts:
                result=prepare_bundle.prepare(episode,second/'review',on_artifact=recovered)
            bundle=json.loads(result.read_text());self.assertTrue(bundle['ready'])
            self.assertEqual(shorts.call_count,2)
            self.assertEqual(bundle['artifacts']['long']['sha256'],hashlib.sha256(source.read_bytes()).hexdigest())

    def test_ready_retry_does_not_render_or_upload_again(self):
        config={'repository':'owner/repo'};store=mock.Mock()
        entry={'asset_id':1,'asset_size':12,'asset_sha256':'a'*64}
        store.find_asset.return_value={'digest':'sha256:'+'a'*64,'size':12}
        with mock.patch.object(production,'daily_store',return_value=store), \
                mock.patch.object(production,'prepared_entry',return_value=entry), \
                mock.patch.object(production,'render_package') as render, \
                mock.patch.object(production,'put_file') as upload:
            result=production.produce(datetime(2026,11,6).date(),config,{},self.root)
        self.assertEqual(result['status'],'already_prepared')
        render.assert_not_called();upload.assert_not_called()

    def test_published_draft_and_incomplete_search_block_creation(self):
        config={'repository':'owner/repo'};api=mock.Mock()
        api.request.return_value=(200,[{'tag_name':'quiet-hours-private-2026-11-06','draft':False}])
        with mock.patch.object(storage,'token',return_value='unit-fixture-token'),mock.patch.object(storage,'GitHubLedger',return_value=api):
            with self.assertRaisesRegex(RunnerError,'published'):
                storage.daily_store(config,datetime(2026,11,6).date(),create=True)
            api.request.return_value=(200,[{'tag_name':'different'}]*100)
            with self.assertRaisesRegex(RunnerError,'bound'):
                storage.daily_store(config,datetime(2026,11,6).date(),create=True)
        self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))

    def test_schedule_activity_is_bounded_and_preserves_unrelated_files(self):
        api=mock.Mock();now=datetime(2026,11,5,tzinfo=timezone.utc)
        data={'schema_version':1,'purpose':'keep-approved-daily-schedule-active','recorded_at':now.isoformat()}
        item={'size':200,'encoding':'base64','content':base64.b64encode(json.dumps(data).encode()).decode(),'sha':'commit-fixture'}
        with mock.patch.object(storage,'token',return_value='unit-fixture-token'),mock.patch.object(storage,'GitHubLedger',return_value=api):
            api.request.return_value=(200,item)
            self.assertEqual(storage.keep_schedule_active({'repository':'owner/repo'},now),'recent_activity_retained')
            self.assertEqual(api.request.call_count,1)
            api.reset_mock();api.request.side_effect=[(404,{}),(201,{'commit':{'sha':'new-fixture'}})]
            self.assertEqual(storage.keep_schedule_active({'repository':'owner/repo'},now),'activity_recorded')
            request=api.request.call_args.args[2]
            self.assertEqual(json.loads(base64.b64decode(request['content'])),data)
            self.assertNotIn('sha',request)
            api.request.side_effect=None;item['content']=base64.b64encode(b'{"purpose":"different"}').decode()
            api.request.return_value=(200,item)
            with self.assertRaisesRegex(RunnerError,'another purpose'):
                storage.keep_schedule_active({'repository':'owner/repo'},now)


if __name__=='__main__':unittest.main()
