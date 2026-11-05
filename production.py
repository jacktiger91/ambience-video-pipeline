#!/usr/bin/env python3
"""Produce tomorrow's fresh ambience package on a free cloud runner.

The job researches explicitly permitted real sources, screens the selected
audio and motion, freezes a dated plan, renders three exports, and retains a
verified private package. Delivery and YouTube publication are separate stages.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import date, datetime, timedelta, timezone
import json
import math
import shutil
import tempfile
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from encrypted_queue import pack_private_bundle, restore_private_bundle
from filebin_delivery import build_manifest
from prepare_bundle import digest
from production_quality import AudioAuditor, QualityError, inspect_visual
from production_render import render_package
from production_sources import FAMILIES, ROTATION, SourceError, audio_candidates, visual_candidates, download
from production_store import Checkpoints, daily_store, prepared_entry, put_file, read_document, reserve_sources, source_history, write_document, keep_schedule_active
from render import probe
from scheduled_runner import RunnerError


class ProductionError(Exception):
    pass


def assemble_edits(candidates: list[dict], duration: float = 3600, crossfade: float = 16, maximum: int = 8) -> list[dict]:
    """Use long, nonoverlapping passages; refuse to loop or pad missing sound."""
    intervals={}
    for item in candidates:
        start,end=item['start'],item['start']+item['duration']
        if not math.isfinite(start) or not math.isfinite(end) or start<0 or item['duration']<=0:
            raise ProductionError('Source intervals must be positive and finite.')
        for a,b in intervals.setdefault(item['sound_id'],[]):
            if start<b and end>a:raise ProductionError('Source intervals overlap; repeated audio is not allowed.')
        intervals[item['sound_id']].append((start,end))
    ordered=sorted(candidates,key=lambda x:-x['duration'])
    result=[];clock=0.0
    for item in ordered:
        needed=duration-clock+(crossfade if result else 0)
        length=min(item['duration'],needed)
        if length<=crossfade:continue
        if len(result)>=maximum:break
        result.append({'sound_id':item['sound_id'],'start':round(item['start'],3),'duration':round(length,3)})
        clock+=length-(crossfade if len(result)>1 else 0)
        if abs(clock-duration)<.002:
            adjustment=duration-sum(x['duration'] for x in result)+crossfade*(len(result)-1)
            result[-1]['duration']=round(result[-1]['duration']+adjustment,3)
            return result
    raise ProductionError('There is not a full hour of distinct, permitted clean audio after transitions. No audio loop or synthetic filler was substituted.')


def choose_plan(day:date,settings:dict,history:dict,work:Path)->dict:
    start=date.fromisoformat(settings['start_date'])
    index=(day-start).days
    if index<0:raise ProductionError('This date precedes automatic production.')
    preferred=ROTATION[index%len(ROTATION)]
    families=list(FAMILIES)
    families=[preferred]+[x for x in families if x!=preferred]
    previous=history['days'].get((day-timedelta(days=1)).isoformat(),{}).get('family')
    families=[x for x in families if x!=previous]
    used=set(history['source_ids']);used_hashes=set(history['media_sha256'])
    work.mkdir(parents=True,exist_ok=True)
    metadata_cache=work/'source-metadata'
    auditor=None
    rejections=[]
    seed=int(day.strftime('%Y%m%d'))
    for family in families:
        # Sources from a rejected concept are disposable; approved bytes and
        # reports are checkpointed separately after the plan is frozen.
        shutil.rmtree(work/'sources',ignore_errors=True)
        print('Researching a fresh '+family+' concept.',flush=True)
        selected_visual=None;visual_review=None
        try:
            visual_pool=visual_candidates(family,metadata_cache,used,seed)
        except SourceError as exc:
            rejections.append({'family':family,'stage':'visual discovery','reason':str(exc)});continue
        for candidate in visual_pool[:settings.get('max_visual_candidates',8)]:
            path=None
            try:
                path=download(candidate,work/'sources')
                sha=digest(path)
                if sha in used_hashes:
                    path.unlink();continue
                report=work/('visual-'+sha[:16]+'.json')
                visual_review=inspect_visual(path,report)
                selected_visual={**candidate,'sha256':sha}
                print('Real camera footage passed full-frame and two-crop motion checks.',flush=True)
                break
            except (SourceError,QualityError) as exc:
                rejections.append({'family':family,'source_id':candidate['id'],'stage':'visual','reason':str(exc)})
                if path is not None:path.unlink(missing_ok=True)
        if selected_visual is None:continue
        candidates=[];sources={};reviews={}
        try:
            audio_pool=audio_candidates(family,metadata_cache,used,seed)
            if auditor is None:auditor=AudioAuditor(work/'models')
        except SourceError as exc:
            rejections.append({'family':family,'stage':'audio discovery','reason':str(exc)});continue
        edits=None
        for number,candidate in enumerate(audio_pool[:settings.get('max_audio_candidates',14)],1):
            print(f'Screening real stereo recording {number} for {family}.',flush=True)
            path=None
            try:
                path=download(candidate,work/'sources')
                sha=digest(path)
                if sha in used_hashes:
                    path.unlink();continue
                info=probe(path)
                report=auditor.audit(path,family,work/('audio-'+family+'-'+sha[:16]+'.json'))
                if not report['clean_intervals']:
                    path.unlink();continue
                sources[candidate['id']]={**candidate,'sha256':sha,'channels':2,
                                         'duration_seconds':float(info['format']['duration']),
                                         'commercial_use_verified':True}
                reviews[candidate['id']]=report
                candidates.extend({'sound_id':candidate['id'],'start':a,'duration':b} for a,b in report['clean_intervals'])
                try:
                    edits=assemble_edits(candidates,maximum=settings.get('max_audio_passages',8))
                    break
                except ProductionError:pass
            except (SourceError,QualityError) as exc:
                rejections.append({'family':family,'source_id':candidate['id'],'stage':'audio','reason':str(exc)})
                if path is not None:path.unlink(missing_ok=True)
        if edits is None:
            rejections.append({'family':family,'stage':'audio','reason':'Not enough distinct clean audio to fill one hour after crossfades.'})
            continue
        selected_ids=list(dict.fromkeys(x['sound_id'] for x in edits))
        theme=selected_visual.get('theme_title') or FAMILIES[family]['themes'][(index//len(ROTATION))%len(FAMILIES[family]['themes'])]
        scene=f"The moving backdrop uses real camera footage by {selected_visual['author']}, titled “{selected_visual['title']}”."
        plan={'schema_version':1,'episode_date':day.isoformat(),'bundle_id':day.isoformat()+'-daily-ambience',
              'family':family,'family_label':FAMILIES[family]['label'],'theme':theme,
              'scene_description':scene,'visual':selected_visual,'visual_review':visual_review,
              'audio_sources':[sources[x] for x in selected_ids],'audio_reviews':{x:reviews[x] for x in selected_ids},
              'edits':edits,'crossfade_seconds':16,'duration_seconds':3600,
              'created_at':datetime.now(timezone.utc).isoformat(),'approval_basis':'User-authorized daily production; commercial source grants and automated quality evidence recorded before rendering.',
              'qa_method':'Automated screening, not subjective listening or a claimed human playback review.'}
        (work/'source-rejections.json').write_text(json.dumps(rejections,indent=2)+'\n')
        (work/'production-plan.json').write_text(json.dumps(plan,indent=2)+'\n')
        return plan
    (work/'source-rejections.json').write_text(json.dumps(rejections,indent=2)+'\n')
    raise ProductionError('No eligible fresh concept passed real-source permission, stereo-audio and moving-scene checks. The dated package remains blocked; no recycled episode or synthetic rain was substituted.')


def produce(day:date,config:dict,settings:dict,root:Path)->dict:
    bundle_id=day.isoformat()+'-daily-ambience'
    store=daily_store(config,day,create=True)
    existing=prepared_entry(store,bundle_id)
    if existing:
        asset=store.find_asset(existing['asset_id'])
        if asset.get('digest')!='sha256:'+existing['asset_sha256'] or asset.get('size')!=existing['asset_size']:
            raise ProductionError('The retained ready archive differs; no replacement was silently generated.')
        return {'status':'already_prepared','episode_date':day.isoformat(),'bundle_id':bundle_id,
                'review_page':'https://github.com/'+config['repository']+'/releases'}
    work=root/day.isoformat();work.mkdir(parents=True,exist_ok=True)
    history_store,history=source_history(config,day,settings['legacy_source_history'])
    plan=read_document(store,'production-plan.json')
    if plan is None:
        local=work/'production-plan.json'
        if local.exists():
            plan=json.loads(local.read_text())
        else:
            plan=choose_plan(day,settings,history,work)
        if plan.get('bundle_id')!=bundle_id:raise ProductionError('A local production plan belongs to another date.')
        write_document(store,'production-plan.json',plan)
    reserve_sources(history_store,history,plan)
    checkpoints=Checkpoints(store,work,bundle_id)
    restored=checkpoints.restore()
    result=render_package(plan,work,on_artifact=checkpoints,restored_source=restored)
    write_document(store,'final-quality-report.json',result['qa'])
    archive=work/'prepared.tar'
    if archive.exists():
        # A retained archive is verified before reuse, never overwritten.
        with tempfile.TemporaryDirectory(prefix='archive-check-',dir=work) as directory:
            check=restore_private_bundle(archive,Path(directory)/'restored',bundle_id=bundle_id)
            _,_,identity=build_manifest(check['bundle_path'],check['backdrop_path'])
        packed={'sha256':digest(archive),'size':archive.stat().st_size,'delivery_identity':identity}
    else:
        packed=pack_private_bundle(result['bundle_path'],archive,backdrop=result['backdrop_path'],motion_report=result['motion_report_path'])
    uploaded=put_file(store,'prepared-'+bundle_id+'.tar',archive)
    entry={'schema_version':1,'bundle_id':bundle_id,'episode_date':day.isoformat(),'theme':plan['theme'],
           'asset_id':uploaded['asset_id'],'asset_size':packed['size'],'asset_sha256':packed['sha256'],
           'delivery_identity':packed['delivery_identity'],'full_decode_all_three':True,
           'source_permissions_verified':True,'actual_scene_motion_checked':True}
    write_document(store,'queue-ready.json',entry)
    release=store.guard()
    body=f"# {day.isoformat()} — {plan['theme']}\n\nProduction is verified and ready for the daily Filebin delivery. The package contains one hour-long video, two vertical Shorts, artwork, titles, descriptions and recording credits.\n\nTarget: ready to download by 18:00 Asia/Nicosia. YouTube upload and publication remain manual.\n\nKeep this release as a private draft. Source permissions, quality evidence and recovery checkpoints are retained in its assets."
    status,saved=store.api.request('PATCH',f'/releases/{store.release_id}',{'body':body,'tag_name':store.tag,'draft':True})
    if status!=200 or saved.get('draft') is not True or saved.get('tag_name')!=store.tag:
        raise ProductionError('Could not confirm the completed package remains private.')
    return {'status':'production_passed','episode_date':day.isoformat(),'bundle_id':bundle_id,'theme':plan['theme'],
            'all_three_fully_decoded':True,'prepared_bytes':packed['size'],
            'review_page':'https://github.com/'+config['repository']+'/releases'}


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('config/scheduled-delivery.json'))
    parser.add_argument('--settings',type=Path,default=Path('config/production-settings.json'))
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--mode',choices=('scheduled','manual','plan-only'),default='scheduled')
    parser.add_argument('--date',help='Explicit date for an authorized manual test; scheduled jobs prepare today and tomorrow.')
    args=parser.parse_args()
    try:
        config=json.loads(args.config.read_text());settings=json.loads(args.settings.read_text())
        if args.mode=='scheduled' and args.date:raise ProductionError('Scheduled production cannot override the Cyprus date.')
        if args.mode!='scheduled' and not args.date:raise ProductionError('Manual production needs an explicit date.')
        if args.mode=='scheduled' and settings.get('enabled') is not True:
            print(json.dumps({'status':'production_disabled'}));return 0
        today=datetime.now(ZoneInfo('Asia/Nicosia')).date()
        days=[date.fromisoformat(args.date)] if args.date else [today,today+timedelta(days=1)]
        days=[x for x in days if x>=date.fromisoformat(settings['start_date'])]
        with contextlib.redirect_stdout(sys.stderr):
            if args.mode=='plan-only':
                history={'schema_version':1,**settings['legacy_source_history'],'days':{}}
                plan=choose_plan(days[0],settings,history,args.work_dir)
                results=[{'status':'plan_verified','bundle_id':plan['bundle_id'],'theme':plan['theme']}]
            else:
                keep_schedule_active(config)
                results=[produce(day,config,settings,args.work_dir) for day in days]
        print(json.dumps({'status':'production_complete','packages':results},indent=2))
        return 0
    except Exception as exc:
        # Network helpers deliberately omit credentials and signed media URLs.
        print('Production stopped: '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
