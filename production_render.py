"""Build one production package from a frozen, licensed daily plan."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import shutil

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from mix_field_recordings import make_mix, measure_levels
from prepare_bundle import prepare, digest, run_ffmpeg
from production_quality import QualityError, inspect_final_motion
from production_sources import download
from render import probe

FONT = Path(__file__).resolve().parent / "assets/fonts/BebasNeue-Regular.ttf"
BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def encoded_loop(source: Path, destination: Path, duration: float) -> dict:
    seconds = math.floor(min(duration, 120) * 30) / 30
    recipe = {"source_sha256": digest(source), "seconds": seconds, "version": 1,
              "format": "1080p30 H.264", "maximum_video_bitrate": 2800000, "crossfade_seconds": 1}
    proof = destination.with_suffix(".json")
    if destination.exists():
        data = json.loads(proof.read_text())
        if data["recipe"] != recipe or data["sha256"] != digest(destination):
            raise QualityError("A retained visual loop differs from the frozen plan.")
        return data
    graph = (f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,trim=duration={seconds:.9f},split=2[a][b];"
             "[a]trim=start=1,setpts=PTS-STARTPTS,fps=30,settb=1/30[body];[b]trim=duration=1,setpts=PTS-STARTPTS,fps=30,settb=1/30[head];"
             f"[body][head]xfade=transition=fade:duration=1:offset={seconds-2:.9f},format=yuv420p[out]")
    pending = destination.with_name(".loop-pending.mp4")
    run_ffmpeg(["-threads", "2", "-i", str(source), "-filter_complex_threads", "2", "-filter_complex", graph,
                "-map", "[out]", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
                "-maxrate", "2800k", "-bufsize", "5600k", "-threads", "2", "-pix_fmt", "yuv420p",
                "-map_metadata", "-1", "-movflags", "+faststart", "-y", str(pending)], timeout=1800)
    info = probe(pending)
    if abs(float(info["format"]["duration"]) - (seconds-1)) > .1:
        raise QualityError("The repeated visual clip has an unexpected duration.")
    run_ffmpeg(["-err_detect", "explode", "-threads", "2", "-i", str(pending), "-f", "null", "-"], timeout=600)
    pending.replace(destination)
    data = {"recipe": recipe, "sha256": digest(destination), "duration": float(info["format"]["duration"]),
            "full_decode": True, "transition": "A one-second visual dissolve joins the real footage. Audio is independent and never follows this short loop."}
    proof.write_text(json.dumps(data, indent=2) + "\n")
    return data


def thumbnail(backdrop: Path, title: str, destination: Path) -> dict:
    """Apply the user's approved recurring cream-title/gold-badge template."""
    if not FONT.is_file() or not BOLD.is_file():
        raise QualityError("The licensed thumbnail fonts are missing.")
    if destination.exists():
        return {"sha256": digest(destination), "method": "fixed channel typography over this episode's actual camera frame"}
    base = Image.open(backdrop).convert("RGB").resize((1280,720), Image.Resampling.LANCZOS).convert("RGBA")
    # Choose a quieter half for lettering, with a subtle local darkening that
    # leaves the photographic subject visible and changes no scene objects.
    gray = np.asarray(base.convert("L"), dtype=float)
    left = float(np.abs(np.diff(gray[:,50:600],axis=1)).mean())
    right = float(np.abs(np.diff(gray[:,680:1230],axis=1)).mean())
    x = 680 if right <= left * 1.2 else 50
    shade = Image.new("RGBA", base.size)
    mask = np.zeros((720,1280,4), dtype=np.uint8)
    for column in range(1280):
        distance = abs(column-(x+275))/420
        mask[:,column,3] = int(100 * max(0,1-distance))
    base = Image.alpha_composite(base, Image.fromarray(mask))
    words = title.upper().replace("’", "'").split()
    if len(words) > 2:
        words = [" ".join(words[:-1]), words[-1]]
    if len(words) == 1:
        words = [words[0]]
    size = 240 if len(words)==2 else 300
    drawing = ImageDraw.Draw(base)
    while size > 100:
        font = ImageFont.truetype(str(FONT),size)
        if max(drawing.textlength(word,font=font) for word in words) <= 550:
            break
        size -= 2
    font = ImageFont.truetype(str(FONT),size)
    top = 205 if len(words)==2 else 265
    shadow = Image.new("RGBA",base.size)
    sd = ImageDraw.Draw(shadow)
    for i,word in enumerate(words):
        box=font.getbbox(word)
        sd.text((x+5,top+i*(size*.9)-box[1]+5),word,font=font,fill=(0,0,0,210),stroke_width=4)
    base=Image.alpha_composite(base,shadow.filter(ImageFilter.GaussianBlur(4)))
    drawing=ImageDraw.Draw(base)
    for i,word in enumerate(words):
        box=font.getbbox(word)
        drawing.text((x,top+i*(size*.9)-box[1]),word,font=font,fill=(249,239,210,255),stroke_width=1)
    badge=Image.new("RGBA",(350,90))
    bd=ImageDraw.Draw(badge)
    for row in range(90):
        shine=math.sin(math.pi*row/90)
        bd.line((0,row,350,row),fill=(int(207+48*shine),int(139+65*shine),int(40+67*shine),255))
    shape=Image.new("L",badge.size);ImageDraw.Draw(shape).rounded_rectangle((0,0,349,89),radius=44,fill=255)
    badge.putalpha(shape)
    base.alpha_composite(badge,(x+80,70))
    drawing=ImageDraw.Draw(base)
    f=ImageFont.truetype(str(BOLD),57)
    drawing.text((x+255,115),"1 HOUR",font=f,fill=(12,11,7,255),anchor="mm")
    base.convert("RGB").save(destination,"JPEG",quality=93,optimize=True)
    return {"sha256":digest(destination),"method":"Automated channel template: actual scene frame, condensed cream title and gold duration badge.","font":"Bebas Neue, SIL Open Font License 1.1"}


def source_credit(source: dict) -> str:
    return f"{source['title']}\nCreator: {source['author']}\n{source['source_page']}\nLicence: {source['license']} — {source['license_url']}"


def short_starts(loop_seconds: float, duration: float, edits: list[dict]) -> list[float]:
    maximum_phase = loop_seconds - duration - 1
    if maximum_phase <= 0:
        raise QualityError("The visual clip cannot fit a Short outside the visual dissolve.")
    phases = [min(3,maximum_phase*.25),min(8,maximum_phase*.75)]
    joins=[];clock=0.0
    for i,edit in enumerate(edits):
        if i: joins.append((clock-16,clock+3));clock-=16
        clock+=edit['duration']
    starts=[]
    for target,phase in zip((900,2700),phases):
        first=math.floor(target/loop_seconds)*loop_seconds+phase
        options=[first+n*loop_seconds for n in range(0,10)]
        options=[s for s in options if s+duration<3597 and not any(s<b and s+duration>a for a,b in joins)]
        if not options: raise QualityError("No distinct Short interval avoids the audio joins.")
        starts.append(round(options[0],6))
    return starts


def render_package(plan: dict, work: Path, *, on_artifact=None, restored_source: Path | None = None) -> dict:
    work.mkdir(parents=True,exist_ok=True)
    sources=[]
    # A checkpointed final hour is authoritative; no source re-render is needed.
    source_video=restored_source
    if source_video is None:
        video=plan['visual']
        visual_path=download(video,work/'sources')
        for source in plan['audio_sources']:
            path=download(source,work/'sources')
            if digest(path)!=source['sha256']: raise QualityError("Selected field recording bytes changed.")
            sources.append({**source,'sound_id':source['id'],'local_path':str(path.resolve()),'commercial_use_verified':True})
        manifest=work/'audio-sources.json';manifest.write_text(json.dumps({'sources':sources},indent=2)+'\n')
        edit_plan=work/'audio-plan.json';edit_plan.write_text(json.dumps({'edits':plan['edits'],'duration_seconds':3600,'crossfade_seconds':16,'theme':plan['theme']},indent=2)+'\n')
        mix=work/'soundtrack.flac'
        print('Arranging distinct real field-recording passages.',flush=True)
        make_mix(manifest,mix,edit_plan)
        # The verified FLAC and its evidence survive; large successful float
        # intermediates need not consume the standard free runner's disk.
        for directory in work.glob('.field-mix-work-*'):
            if directory.is_dir() and not directory.is_symlink():shutil.rmtree(directory)
        print('Encoding the fixed moving scene.',flush=True)
        loop=work/'visual-loop.mp4'
        loop_info=encoded_loop(visual_path,loop,plan['visual_review']['duration'])
        shutil.rmtree(work/'sources')
        source_video=work/'long-source.mp4'
        if not source_video.exists():
            pending=work/'.long-pending.mp4'
            run_ffmpeg(['-stream_loop','-1','-threads','2','-i',str(loop),'-threads','2','-i',str(mix),
                        '-map','0:v:0','-map','1:a:0','-t','3600','-c:v','copy','-c:a','aac','-b:a','192k','-ac','2','-ar','48000',
                        '-map_metadata','-1','-map_chapters','-1','-movflags','+faststart','-y',str(pending)],timeout=1800)
            pending.replace(source_video)
        plan['rendered_loop_seconds']=loop_info['duration']
    # Use the exact loop duration saved with the plan/checkpoint. CFR rounding
    # is known before generation, so the clip windows are stable across retries.
    loop_seconds=plan.get('rendered_loop_seconds',math.floor(min(plan['visual_review']['duration'],120)*30)/30-1)
    short_duration=25 if loop_seconds>=29 else 15
    starts=short_starts(loop_seconds,short_duration,plan['edits'])
    backdrop=work/'backdrop.jpg'
    if not backdrop.exists():
        run_ffmpeg(['-ss','5','-i',str(source_video),'-map','0:v:0','-frames:v','1','-vf','scale=1920:1080',
                    '-q:v','2','-threads','1','-y',str(backdrop)],timeout=120)
    art=work/'thumbnail-source.jpg'
    thumbnail(backdrop,plan['theme'],art)
    credits='\n\n'.join(source_credit(x) for x in plan['audio_sources'])
    credits+='\n\nAudio changes: distinct, non-overlapping field-recording passages, level matching, 16-second transitions and comfortable loudness adjustment. No short audio loop.\n\nVisual recording:\n'+source_credit(plan['visual'])
    credits+='\nVisual changes: original sound removed; a fixed crop, video compression and a brief repeating visual dissolve. The separately recorded soundtrack is not synchronous location sound.\nThumbnail: frame from the same licensed footage with the Quiet Hours title template; Bebas Neue font under SIL OFL 1.1.'
    family=plan['family_label']
    episode={'bundle_id':plan['bundle_id'],'manual_upload_only':True,'intended_channel_id':None,'intended_channel_handle':'@QuietHoursRelaxingSounds',
             'theme':plan['theme'],'long_video':str(source_video.resolve()),'thumbnail_path':str(art.resolve()),
             'title':f"{plan['theme']} | 1 Hour of {family}",
             'description':f"Settle into {plan['theme'].lower()} with an hour of real stereo field recordings and a naturally moving, fixed-camera scene.\n\n{plan['scene_description']}\n\nThe pictures and soundtrack were recorded separately. Long, distinct audio passages are joined gently; the camera clip repeats with a brief visual dissolve. No music.\n\nEnjoy a quiet hour for reading, relaxing or taking a break.\n\n#NatureSounds #Ambience #QuietHours",
             'recording_credits':credits,'made_for_kids':False,'contains_synthetic_media':False,'playlist_id':None,
             'shorts':[]}
    for i,(start,crop) in enumerate(zip(starts,plan['visual_review']['short_crops']),1):
        episode['shorts'].append({'title':(f"A Quiet Moment — {plan['theme']}" if i==1 else f"Pause Here for {family}"),
                                 'description':f"A different view of {plan['theme'].lower()}, with real {family.lower()}. Watch the full hour on Quiet Hours Relaxing Sounds.\n\nThe filmed scene and field recordings come from separate sources, credited below.\n\n#Shorts #NatureSounds #QuietHours",
                                 'start':start,'duration':short_duration,'framing':'center','crop_position':crop['position'],
                                 'cover_offset':short_duration-3,'cta':'quiet-hours','recording_credits':credits})
    episode_path=work/'episode.json';episode_path.write_text(json.dumps(episode,indent=2)+'\n')
    print('Preparing three exports and verifying every stream through the end.',flush=True)
    bundle_path=prepare(episode_path,work/'review',on_artifact=on_artifact)
    if source_video==work/'long-source.mp4':source_video.unlink()
    bundle=json.loads(bundle_path.read_text())
    motion_path=work/'motion-review.json';inspect_final_motion(bundle,motion_path)
    levels={}
    for video in bundle['videos']:
        measured=measure_levels(Path(video['path']))
        if not -32<=float(measured['input_i'])<=-16 or float(measured['input_tp'])>-2:
            raise QualityError('An exported soundtrack failed comfortable loudness/headroom limits.')
        levels[video['key']]=measured
    qa={'bundle_id':plan['bundle_id'],'audio_levels':levels,'full_decode_all_three':True,'motion_report':json.loads(motion_path.read_text()),
        'method':'Automated full-file decode, stream validation, measured audio levels, temporal motion checks and licensed-source records; no fabricated listening approval.'}
    (work/'final-qa.json').write_text(json.dumps(qa,indent=2)+'\n')
    return {'bundle_path':bundle_path,'backdrop_path':backdrop,'motion_report_path':motion_path,'qa':qa,'episode':episode}
