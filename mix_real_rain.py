#!/usr/bin/env python3
"""Arrange verified stereo field recordings into a nonrepeating one-hour mix."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile


TARGET_I = -22.0
TARGET_TP = -4.0  # Leave headroom for the subsequent AAC encode.
CROSSFADE = 16.0
PLAN = [
    {"sound_id": "543481", "start": 20.0, "duration": 330.0},
    {"sound_id": "430765", "start": 25.0, "duration": 1250.0},
    {"sound_id": "733979", "start": 30.0, "duration": 1150.0},
    {"sound_id": "616991", "start": 30.0, "duration": 918.0},
]


class MixError(Exception):
    pass


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(arguments: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        raise MixError('Media command failed: ' + result.stderr[-2000:])
    return result


def ffmpeg(arguments: list[str]) -> subprocess.CompletedProcess:
    return run(['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'info',
                '-filter_threads', '2', '-filter_complex_threads', '2', *arguments])


def probe(path: Path) -> dict:
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]).stdout)


def loudness(path: Path, start: float = 0, duration: float | None = None) -> dict:
    arguments = ['-ss', str(start), '-threads', '2', '-i', str(path)]
    if duration is not None:
        arguments += ['-t', str(duration)]
    arguments += ['-map', '0:a:0', '-af', f'loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA=11:print_format=json', '-f', 'null', '-']
    result = ffmpeg(arguments)
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr, re.S)
    if not matches:
        raise MixError('FFmpeg did not report loudness measurements.')
    data = json.loads(matches[-1])
    if not all(math.isfinite(float(data[k])) for k in ['input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset']):
        raise MixError('Invalid or silent soundtrack segment.')
    return data


def measure_levels(path: Path, start: float = 0, duration: float | None = None) -> dict:
    """Measure EBU R128 levels without spending CPU normalizing a discarded copy."""
    arguments = ['-ss', str(start), '-threads', '2', '-i', str(path)]
    if duration is not None:
        arguments += ['-t', str(duration)]
    arguments += ['-map', '0:a:0', '-af', 'ebur128=peak=true:framelog=verbose', '-f', 'null', '-']
    result = ffmpeg(arguments)
    summary = result.stderr.rsplit('Summary:', 1)[-1]
    patterns = {'input_i': r'Integrated loudness:\s*I:\s*([-+\d.]+)',
                'input_tp': r'True peak:\s*Peak:\s*([-+\d.]+)',
                'input_lra': r'Loudness range:\s*LRA:\s*([-+\d.]+)',
                'input_thresh': r'Integrated loudness:.*?Threshold:\s*([-+\d.]+)'}
    data = {'measurement': 'FFmpeg ebur128 with true-peak oversampling'}
    for key, pattern in patterns.items():
        match = re.search(pattern, summary, re.S)
        if not match or not math.isfinite(float(match.group(1))):
            raise MixError('Invalid EBU R128 level measurement.')
        data[key] = match.group(1)
    return data


def validate_plan(sources: dict, plan: list[dict], crossfade: float) -> list[dict]:
    intervals = {}
    timeline = 0.0
    checked = []
    for index, edit in enumerate(plan):
        source = sources.get(edit['sound_id'])
        if not source or source.get('license') not in {'CC0-1.0', 'CC-BY-4.0'} or source.get('commercial_use_verified') is not True:
            raise MixError('Every source needs verified commercial-use permission before rendering.')
        if source.get('field_recording_verified_from_creator_description') is not True:
            raise MixError('Every source must be a documented real field recording.')
        if source.get('channels') != 2:
            raise MixError('Only real stereo source recordings are accepted.')
        start, duration = float(edit['start']), float(edit['duration'])
        if start < 0 or duration <= crossfade or start + duration > float(source['duration_seconds']) + 0.05:
            raise MixError('Source passage is out of bounds or too short for its crossfade.')
        key = source['sha256']
        for earlier_start, earlier_end in intervals.setdefault(key, []):
            if start < earlier_end and start + duration > earlier_start:
                raise MixError('A source passage repeats; select non-overlapping intervals.')
        intervals[key].append((start, start + duration))
        if index:
            timeline -= crossfade
        checked.append({**edit, 'timeline_start': timeline, 'timeline_end': timeline + duration,
                        'source_start': start, 'source_end': start + duration,
                        'author': source['author'], 'source_page': source['source_page'],
                        'license': source['license'], 'license_url': source['license_url'],
                        'source_sha256': key, 'path': source['local_path']})
        timeline += duration
    return checked


def mix_segments(edits: list[dict], gains: list[float], crossfade: float, destination: Path) -> None:
    args = []
    filters = []
    for index, (edit, gain) in enumerate(zip(edits, gains)):
        args += ['-ss', str(edit['source_start']), '-t', str(edit['duration']), '-threads', '2', '-i', edit['path']]
        filters.append(f'[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,volume={gain:.6f}dB[a{index}]')
    last = 'a0'
    for index in range(1, len(edits)):
        out = f'blend{index}'
        filters.append(f'[{last}][a{index}]acrossfade=d={crossfade}:c1=qsin:c2=qsin[{out}]')
        last = out
    duration = edits[-1]['timeline_end']
    fade = min(3.0, duration / 4)
    filters.append(f'[{last}]afade=t=in:d={fade},afade=t=out:st={duration-fade}:d={fade}[mix]')
    # Gain matching can put a thunder transient above 0 dBFS. Float PCM keeps
    # those peaks intact until the measured final normalization/limiter.
    codec = ['-c:a', 'pcm_f32le'] if destination.suffix == '.wav' else ['-c:a', 'flac', '-compression_level', '5']
    ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '[mix]', *codec, '-ar', '48000', '-ac', '2', str(destination)])


def make_mix(manifest: Path, output: Path) -> dict:
    data = json.loads(manifest.read_text())
    sources = {record['sound_id']: record for record in data['sources']}
    edits = validate_plan(sources, PLAN, CROSSFADE)
    if abs(edits[-1]['timeline_end'] - 3600) > 0.001:
        raise MixError('Production edit must be exactly one hour.')
    recipe = {'edits': edits, 'crossfade_seconds': CROSSFADE, 'target_lufs': TARGET_I, 'target_true_peak': TARGET_TP, 'floating_point_intermediate': True, 'version': 3}
    report_path = output.with_suffix('.mix.json')
    if output.exists():
        if report_path.exists():
            existing = json.loads(report_path.read_text())
            if existing.get('recipe') == recipe and existing.get('sha256') == sha256(output):
                print('Reusing verified real-rain soundtrack:', output)
                return existing
        raise MixError('Existing output differs or lacks verification; choose a new path.')
    for edit in edits:
        path = Path(edit['path'])
        if sha256(path) != edit['source_sha256']:
            raise MixError('A licensed source file changed; refusing to render.')
        info = probe(path)
        audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
        if len(audio) != 1 or audio[0]['channels'] != 2:
            raise MixError('A source is no longer a stereo audio file.')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Retain work and diagnostics after a rejected normalization pass. A retry
    # must not discard and remeasure the same hour of source audio.
    recipe_key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]
    directory = output.parent / f'.real-rain-work-{recipe_key}'
    directory.mkdir(exist_ok=True)
    with nullcontext(directory) as directory:
        temp = Path(directory)
        metrics = []
        gains = []
        metrics_path = temp / 'source-levels.json'
        if metrics_path.exists():
            cached = json.loads(metrics_path.read_text())
            if cached['recipe'] != recipe:
                raise MixError('Cached source measurements have a different recipe.')
            metrics = cached['metrics']
            gains = cached['gains']
        else:
            for edit in edits:
                print('Measuring source passage:', edit['sound_id'], flush=True)
                measured = measure_levels(Path(edit['path']), edit['source_start'], edit['duration'])
                metrics.append(measured)
                gains.append(-24.0 - float(measured['input_i']))
            metrics_path.write_text(json.dumps({'recipe': recipe, 'metrics': metrics, 'gains': gains}, indent=2))
        pre = temp / 'assembled.wav'
        if not pre.exists():
            print('Blending four unique field-recording passages; no repeated intervals.', flush=True)
            pending = temp / 'assembled-pending.wav'
            if pending.exists():
                pending.unlink()
            mix_segments(edits, gains, CROSSFADE, pending)
            pending.rename(pre)
        measure_path = temp / 'assembled-levels.json'
        if measure_path.exists():
            cached = json.loads(measure_path.read_text())
            if cached['sha256'] != sha256(pre):
                raise MixError('Cached float assembly changed.')
            measure = cached['levels']
        else:
            measure = measure_levels(pre)
            measure_path.write_text(json.dumps({'sha256': sha256(pre), 'levels': measure}, indent=2))
        normalized = temp / 'normalized.flac'
        gain = TARGET_I - float(measure['input_i'])
        ceiling = 10 ** ((TARGET_TP - 0.5) / 20)
        norm = (f'volume={gain:.6f}dB,aresample=192000,'
                f'alimiter=limit={ceiling:.9f}:level=false:latency=true:attack=5:release=200,'
                'aresample=48000')
        print('Applying measured comfortable loudness with headroom for AAC.', flush=True)
        if not normalized.exists():
            pending = temp / 'normalized-pending.flac'
            if pending.exists():
                pending.unlink()
            result = ffmpeg(['-threads', '2', '-i', str(pre), '-map', '0:a:0', '-af', norm, '-ar', '48000', '-ac', '2', '-c:a', 'flac', '-compression_level', '5', str(pending)])
            (temp / 'normalization-log.txt').write_text(result.stderr)
            pending.rename(normalized)
        final = measure_levels(normalized)
        pre_trim_levels = final
        trim_gain = min(max(0.0, TARGET_I - float(final['input_i'])),
                        max(0.0, TARGET_TP - float(final['input_tp'])))
        if trim_gain > 0.05:
            adjusted = temp / 'gain-adjusted.flac'
            if not adjusted.exists():
                ffmpeg(['-threads', '2', '-i', str(normalized), '-af', f'volume={trim_gain:.6f}dB',
                        '-c:a', 'flac', '-compression_level', '5', str(adjusted)])
            normalized = adjusted
            final = measure_levels(normalized)
        (temp / 'final-levels.json').write_text(json.dumps(final, indent=2))
        print('Final measured levels:', json.dumps(final), flush=True)
        if not -24.0 <= float(final['input_i']) <= -20.0 or float(final['input_tp']) > -3.0:
            raise MixError(f'Normalized soundtrack failed comfortable loudness or true-peak limits; retained diagnostics: {temp}; levels: {final}')
        info = probe(normalized)
        if abs(float(info['format']['duration']) - 3600) > 0.05:
            raise MixError('Final soundtrack is not one hour.')
        ffmpeg(['-v', 'error', '-xerror', '-threads', '2', '-i', str(normalized), '-map', '0:a:0', '-f', 'null', '-'])
        report = {'recipe': recipe, 'source_loudness': metrics, 'source_gain_db': gains,
                  'assembled_loudness': measure, 'final_loudness': final,
                  'normalization_method': 'Measured constant gain plus oversampled lookahead peak limiter; no automatic gain riding',
                  'normalization_filter': norm,
                  'pre_trim_loudness': pre_trim_levels, 'final_constant_gain_db': trim_gain,
                  'duration_seconds': 3600, 'sample_rate': 48000, 'channels': 2,
                  'comfortable_level_acceptance': {'integrated_lufs': [-24, -20], 'maximum_true_peak_db': -3},
                  'full_decode_passed': True, 'no_source_interval_repeats': True,
                  'auditory_review': 'Automated full decode, loudness, source provenance and interval checks. Human listening remains recommended; these checks do not establish subjective quality.',
                  'sha256': sha256(normalized)}
        # Hard-link publication keeps both soundtrack and sidecar out of existing files.
        import os
        os.link(normalized, output)
        with report_path.open('x') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    print('Verified real-rain soundtrack:', output, flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources-manifest', type=Path, default=Path('docs/provenance/revised-night-rain/sources.json'))
    parser.add_argument('--output', type=Path, default=Path('media/revised-night-rain/soundtrack.flac'))
    args = parser.parse_args()
    try:
        make_mix(args.sources_manifest, args.output)
    except (MixError, OSError, ValueError) as exc:
        print('Error:', exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
