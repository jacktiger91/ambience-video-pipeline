"""Private per-day cloud storage and durable production checkpoints.

One draft per dated package keeps receipt/checkpoint counts bounded, while
preserving earlier packages. No review release is ever published or deleted.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import re
import tempfile

from cloud_ledger import GitHubLedger
from scheduled_runner import DraftStore, RunnerError, token
from scheduled_bootstrap import upload_private_file


def private_store(config: dict, tag: str, name: str, *, create: bool = False) -> DraftStore | None:
    if not re.fullmatch(r"quiet-hours-private-(?:\d{4}-\d{2}-\d{2}|history-\d{4})", tag):
        raise RunnerError("Unexpected daily private-storage identity.")
    api = GitHubLedger(config['repository'], token())
    found=[];complete=False
    for page in range(1,21):
        status, releases=api.request('GET',f'/releases?per_page=100&page={page}')
        if status!=200 or not isinstance(releases,list): raise RunnerError('Could not inspect dated private production storage.')
        found.extend(x for x in releases if x.get('tag_name')==tag)
        if found or len(releases)<100:
            complete=True;break
    if not complete:raise RunnerError('Production storage search reached its bound; no duplicate draft was created.')
    if len(found)>1: raise RunnerError('More than one release has the intended private production identity.')
    if found:
        release=found[0]
        if release.get('draft') is not True: raise RunnerError('The production release was published; no media write may proceed.')
    elif not create:
        return None
    else:
        status,release=api.request('POST','/releases',{'tag_name':tag,'target_commitish':'main','name':name,'draft':True,
            'body':'# Quiet Hours private production\n\nThis draft holds a dated review package and its recovery records. Keep it unpublished. YouTube upload and publishing remain manual.'})
        if status!=201 or not isinstance(release,dict) or release.get('draft') is not True:
            raise RunnerError('Could not create protected production storage.')
        # GitHub can substitute an untagged name if PATCH omits this field.
        status,release=api.request('PATCH',f"/releases/{release['id']}",{'tag_name':tag,'draft':True,'name':name})
        if status!=200 or release.get('tag_name')!=tag or release.get('draft') is not True:
            raise RunnerError('Private production storage identity could not be confirmed.')
    store=DraftStore({**config,'private_release_id':release['id'],'private_release_tag':tag},token())
    store.guard()
    if create: store.anonymous_probe()
    return store


def daily_store(config: dict, day: date, *, create: bool = False) -> DraftStore | None:
    return private_store(config,'quiet-hours-private-'+day.isoformat(),'Quiet Hours — '+day.isoformat(),create=create)


def read_document(store: DraftStore, name: str) -> dict | None:
    matches=[x for x in store.assets() if x.get('name')==name]
    if not matches: return None
    if len(matches)!=1 or matches[0].get('state')!='uploaded' or not 0<matches[0].get('size',0)<=8*1024**2:
        raise RunnerError('A private production document is duplicated, incomplete or oversized.')
    item=matches[0]
    expected=item.get('digest','') or ''
    expected=expected.removeprefix('sha256:') if expected.startswith('sha256:') else None
    with tempfile.TemporaryDirectory(prefix='production-document-') as temporary:
        p=Path(temporary)/'document.json';store.download(item,p,expected,item['size'])
        value=json.loads(p.read_text())
    if not isinstance(value,dict): raise RunnerError('Private production documents must be JSON objects.')
    return value


def put_file(store: DraftStore, name: str, path: Path) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}',name): raise RunnerError('Invalid private asset filename.')
    with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    size=path.stat().st_size
    matches=[x for x in store.assets() if x.get('name')==name]
    if matches:
        if len(matches)!=1: raise RunnerError('Duplicate private assets require review.')
        item=matches[0]
        if item.get('state')!='uploaded' or item.get('size')!=size or item.get('digest')!='sha256:'+sha:
            raise RunnerError('Existing private media differs or is incomplete; it was not overwritten.')
    else:
        item=upload_private_file(store,path,name,sha)
    return {'asset_id':item['id'],'size':size,'sha256':sha,'name':name}


def write_document(store: DraftStore, name: str, value: dict) -> dict:
    body=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
    if len(body)>8*1024**2: raise RunnerError('Private production metadata exceeds its bound.')
    with tempfile.TemporaryDirectory(prefix='production-metadata-') as temporary:
        p=Path(temporary)/'data.json';p.write_bytes(body)
        return put_file(store,name,p)


def prepared_entry(store: DraftStore, bundle_id: str) -> dict | None:
    entry=read_document(store,'queue-ready.json')
    if entry is None:return None
    if (entry.get('schema_version')!=1 or entry.get('bundle_id')!=bundle_id
            or type(entry.get('asset_id')) is not int or not 0<entry.get('asset_size',0)<2*1024**3
            or not re.fullmatch(r'[a-f0-9]{64}',entry.get('asset_sha256',''))
            or not re.fullmatch(r'[a-f0-9]{64}',entry.get('delivery_identity',''))
            or any(entry.get(key) is not True for key in ('full_decode_all_three','source_permissions_verified','actual_scene_motion_checked'))):
        raise RunnerError('The private prepared entry does not identify a verified package for this date.')
    return entry


def source_history(config: dict, day: date, seeds: dict) -> tuple[DraftStore,dict]:
    store=private_store(config,f'quiet-hours-private-history-{day.year}',f'Quiet Hours source history {day.year}',create=True)
    pattern=re.compile(r'history-(\d{4}-\d{2}-\d{2})\.json')
    names=sorted(x['name'] for x in store.assets() if pattern.fullmatch(x.get('name','')))
    if names:
        data=read_document(store,names[-1])
    else:
        old=private_store(config,f'quiet-hours-private-history-{day.year-1}',f'Quiet Hours source history {day.year-1}')
        older=sorted(x['name'] for x in old.assets() if pattern.fullmatch(x.get('name',''))) if old else []
        data=read_document(old,older[-1]) if older else {'schema_version':1,'source_ids':seeds.get('source_ids',[]),'media_sha256':seeds.get('media_sha256',[]),'days':{}}
    if (data.get('schema_version')!=1 or not isinstance(data.get('source_ids'),list)
            or not isinstance(data.get('media_sha256'),list) or not isinstance(data.get('days'),dict)):
        raise RunnerError('Source history cannot be verified; do not silently reuse media.')
    return store,data


def reserve_sources(store: DraftStore, history: dict, plan: dict) -> None:
    value=json.loads(json.dumps(history));day=plan['episode_date']
    records=[plan['visual'],*plan['audio_sources']]
    detail={'family':plan['family'],'theme':plan['theme'],'bundle_id':plan['bundle_id'],'source_ids':[x['id'] for x in records]}
    if day in value['days']:
        if value['days'][day]!=detail: raise RunnerError('This date already reserved different production sources.')
        return
    value['days'][day]=detail
    value['source_ids']=sorted(set(value['source_ids'])|{x['id'] for x in records})
    value['media_sha256']=sorted(set(value['media_sha256'])|{x['sha256'] for x in records})
    write_document(store,'history-'+day+'.json',value)


class Checkpoints:
    def __init__(self,store:DraftStore,work:Path,bundle_id:str):
        self.store,self.work,self.bundle_id=store,work,bundle_id
        names=sorted(x['name'] for x in store.assets() if re.fullmatch(r'checkpoint-\d{6}\.json',x.get('name','')))
        self.version=int(names[-1][11:17]) if names else 0
        self.data=read_document(store,names[-1]) if names else {'schema_version':1,'bundle_id':bundle_id,'assets':{},'manifest':None}
        if self.data.get('bundle_id')!=bundle_id:raise RunnerError('Checkpoint belongs to a different dated production.')

    def restore(self) -> Path | None:
        if not self.data['assets']: return None
        for key,record in self.data['assets'].items():
            if key not in {'long','short1','short2','thumbnail','short1-cover','short2-cover','backdrop','thumbnail-source'}:
                raise RunnerError('Unexpected checkpoint media key.')
            filename=record['filename']
            if filename!=Path(filename).name or not re.fullmatch(r'[A-Za-z0-9_.-]+',filename):raise RunnerError('Invalid checkpoint filename.')
            directory=self.work if key in {'backdrop','thumbnail-source'} else self.work/'review'
            target=directory/filename
            self.store.download(self.store.find_asset(record['asset_id']),target,record['sha256'],record['size'])
        manifest=self.data['manifest']
        if manifest:
            for key,record in manifest['artifacts'].items():
                record['path']=str((self.work/'review'/self.data['assets'][key]['filename']).resolve())
            (self.work/'review'/'bundle.json').write_text(json.dumps(manifest,indent=2)+'\n')
        return self.work/'review'/'long.mp4' if 'long' in self.data['assets'] else None

    def __call__(self,key: str,path: Path,record:dict,manifest:dict) -> None:
        for extra in ('backdrop','thumbnail-source'):
            if extra not in self.data['assets']:
                p=self.work/(extra+'.jpg')
                with p.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
                value=put_file(self.store,'source-'+extra+'-'+sha[:16]+'.jpg',p)
                self.data['assets'][extra]={**value,'filename':p.name}
        value=put_file(self.store,'verified-'+key+'-'+record['sha256'][:16]+path.suffix,path)
        self.data['assets'][key]={**value,'filename':path.name}
        self.data['manifest']=json.loads(json.dumps(manifest))
        for item in self.data['manifest']['artifacts'].values():item['path']=Path(item['path']).name
        self.version+=1
        write_document(self.store,f'checkpoint-{self.version:06d}.json',self.data)
