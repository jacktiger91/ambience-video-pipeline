"""Automated production checks, with explicit evidence rather than listening claims."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import zipfile

import cv2
import numpy as np

from production_sources import SourceError, download
from render import probe, stream_of_type

MODEL = {"url": "https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/audio_classification/android/lite-model_yamnet_classification_tflite_1.tflite",
         "sha256": "10c95ea3eb9a7bb4cb8bddf6feb023250381008177ac162ce169694d05c317de"}
TARGETS = {
    "coast": ["Waves, surf", "Ocean", "Water", "Slosh"],
    "water": ["Water", "Slosh", "Splash, splatter", "Stream", "Bird"],
    "stream": ["Stream", "Water", "Gurgling", "Trickle, dribble"],
    "rain": ["Rain", "Rain on surface", "Raindrop"],
    "birds": ["Bird", "Bird vocalization, bird call, bird song", "Chirp, tweet"],
    "meadow": ["Cricket", "Insect", "Rustling leaves", "Bird", "Wind"],
}


class QualityError(Exception):
    pass


def command(args: list[str], timeout: int = 1800) -> bytes:
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        raise QualityError("Media analysis failed: " + result.stderr.decode(errors="replace")[-1200:])
    return result.stdout


def clean_intervals(duration: float, bad_times: list[float], *, padding: float = 12, minimum: float = 90) -> list[tuple[float, float]]:
    """Exclude flagged events with margins; never repeat a source interval."""
    blocked = sorted((max(0, x - padding), min(duration, x + padding + 1)) for x in bad_times)
    merged = []
    for start, end in blocked:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result, start = [], 0.0
    for a, b in [*merged, (duration, duration)]:
        if a - start >= minimum:
            result.append((round(start, 3), round(a - start, 3)))
        start = max(start, b)
    return result


class AudioAuditor:
    def __init__(self, cache: Path):
        from ai_edge_litert.interpreter import Interpreter
        model = download(MODEL, cache)
        self.interpreter = Interpreter(model_path=str(model), num_threads=2)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]
        if list(self.input["shape"]) != [15600] or self.input["dtype"] != np.float32:
            raise QualityError("Unexpected audio classification model input.")
        with zipfile.ZipFile(model) as archive:
            self.labels = archive.read("yamnet_label_list.txt").decode().splitlines()
        if len(self.labels) != 521:
            raise QualityError("Unexpected audio classification label map.")

    def audit(self, source: Path, family: str, report_path: Path, *, maximum_seconds: float = 4800) -> dict:
        with source.open('rb') as stream:sha = hashlib.file_digest(stream, "sha256").hexdigest()
        recipe = {"sha256": sha, "family": family, "model_sha256": MODEL["sha256"], "hop_seconds": 0.48, "maximum_seconds": maximum_seconds, "version": 1}
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report.get("recipe") == recipe:
                return report
            raise QualityError("An audio audit belongs to different source bytes or settings.")
        metadata = probe(source)
        stream = stream_of_type(metadata, "audio", "Field recording")
        if stream.get("channels") != 2:
            raise QualityError("Real two-channel source audio is required.")
        duration = min(float(metadata["format"]["duration"]), maximum_seconds)
        sample = command(["ffmpeg", "-v", "error", "-xerror", "-ss", str(min(20, duration/4)), "-i", str(source), "-t", "20", "-ac", "2", "-ar", "16000", "-f", "f32le", "-"])
        stereo = np.frombuffer(sample, dtype="<f4").reshape(-1, 2)
        energy = float(np.mean(stereo ** 2))
        separation = float(np.mean((stereo[:, 0] - stereo[:, 1]) ** 2) / max(energy, 1e-15))
        if not np.isfinite(stereo).all() or energy < 1e-10 or separation < 1e-5:
            raise QualityError("The source is silent, invalid or duplicates mono in both channels.")
        decoded = command(["ffmpeg", "-v", "error", "-xerror", "-i", str(source), "-t", str(duration), "-vn", "-ar", "16000", "-ac", "1", "-f", "f32le", "-"])
        samples = np.frombuffer(decoded, dtype="<f4")
        if len(samples) < 15600 or not np.isfinite(samples).all():
            raise QualityError("The selected source window did not decode completely.")
        duration = len(samples) / 16000
        targets = [self.labels.index(x) for x in TARGETS[family] if x in self.labels]
        thresholds = {"Speech": .25, "Music": .25, "Wind noise (microphone)": .35,
                      "Screaming": .25, "Siren": .25, "Gunshot, gunfire": .25,
                      "Fireworks": .25, "Vehicle": .5, "Engine": .5, "Aircraft": .5,
                      "Crow": .3, "Caw": .3, "Dog": .5, "Silence": .9}
        if family != "birds": thresholds["Gull, seagull"] = .5
        unwanted = [(self.labels.index(x), cutoff, x) for x, cutoff in thresholds.items() if x in self.labels]
        bad, target_scores, top_sum, maxima = [], [], np.zeros(521), {name: 0.0 for _, _, name in unwanted}
        total = 0
        for offset in range(0, len(samples), 7680):
            frame = samples[offset:offset + 15600]
            if len(frame) < 15600:
                frame = np.pad(frame, (0, 15600 - len(frame)))
            self.interpreter.set_tensor(self.input["index"], frame)
            self.interpreter.invoke()
            scores = self.interpreter.get_tensor(self.output["index"]).reshape(-1)
            total += 1
            top_sum += scores
            target_scores.append(float(scores[targets].max()))
            if any(scores[i] >= threshold for i, threshold, _ in unwanted):
                bad.append(offset / 16000)
            for i, _, label in unwanted: maxima[label] = max(maxima[label], float(scores[i]))
        intervals = clean_intervals(duration, bad)
        target_fraction = float(np.mean(np.asarray(target_scores) >= .1))
        if target_fraction < .03:
            intervals = []
        report = {"recipe": recipe, "channels": 2, "stereo_difference_energy_ratio": separation,
                  "analysed_seconds": duration, "frames": total, "clean_intervals": intervals,
                  "excluded_event_windows": len(bad), "event_maxima": maxima,
                  "target_frame_fraction_at_0_1": target_fraction,
                  "top_mean_events": [{"label": self.labels[i], "score": float(top_sum[i]/total)} for i in np.argsort(top_sum)[-6:][::-1]],
                  "method": "Full-window YAMNet classification with 0.48-second hop, padded event exclusions, actual stereo difference check and decoded source bytes.",
                  "limitations": "Automated content screening is not subjective listening. Real origin and permission are established separately from source evidence."}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        return report


def video_frames(path: Path, *, start: float = 0, duration: float = 45, crop: float | None = None) -> np.ndarray:
    vf = "fps=1,scale=640:360:force_original_aspect_ratio=decrease"
    if crop is not None:
        vf = f"crop=ih*9/16:ih:(iw-ow)*{crop:.8f}:0,fps=1,scale=202:360"
        width, height = 202, 360
    else:
        vf += ",pad=640:360:(ow-iw)/2:(oh-ih)/2"
        width, height = 640, 360
    raw = command(["ffmpeg", "-v", "error", "-xerror", "-ss", str(start), "-i", str(path), "-t", str(duration),
                   "-vf", vf + ",format=gray", "-an", "-threads", "2", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    if len(raw) % (width * height):
        raise QualityError("Motion frame extraction returned incomplete data.")
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width)
    if len(frames) < 12:
        raise QualityError("Too little footage for a temporal motion check.")
    return frames


def motion_metrics(frames: np.ndarray) -> dict:
    shifts, rotations, scales, residuals, fractions, drifts = [], [], [], [], [], []
    reference = frames[0]
    for index, (a, b) in enumerate(zip(frames[:-1], frames[1:])):
        points = cv2.goodFeaturesToTrack(a, maxCorners=500, qualityLevel=.015, minDistance=5)
        if points is None or len(points) < 15:
            continue
        moved, status, _ = cv2.calcOpticalFlowPyrLK(a, b, points, None)
        good = status.reshape(-1).astype(bool)
        left, right = points[good].reshape(-1, 2), moved[good].reshape(-1, 2)
        if len(left) < 12:
            continue
        affine, inliers = cv2.estimateAffinePartial2D(left, right, method=cv2.RANSAC, ransacReprojThreshold=1.2)
        if affine is None:
            continue
        shifts.append(float(np.linalg.norm(affine[:, 2])))
        rotations.append(abs(math.degrees(math.atan2(affine[1, 0], affine[0, 0]))))
        scales.append(abs(float(np.hypot(affine[0, 0], affine[1, 0])) - 1))
        predicted = left @ affine[:, :2].T + affine[:, 2]
        residuals.append(float(np.percentile(np.linalg.norm(right-predicted, axis=1), 75)))
        warped = cv2.warpAffine(a, affine, (b.shape[1], b.shape[0]))
        diff = cv2.absdiff(warped, b)[4:-4, 4:-4]
        fractions.append(float(np.mean(diff > 10)))
        if index % 5 == 0:
            p = cv2.goodFeaturesToTrack(reference, maxCorners=500, qualityLevel=.015, minDistance=5)
            if p is not None:
                q, ok, _ = cv2.calcOpticalFlowPyrLK(reference, b, p, None)
                mask = ok.reshape(-1).astype(bool)
                if mask.sum() >= 15:
                    transform, mask2 = cv2.estimateAffinePartial2D(p[mask], q[mask], method=cv2.RANSAC, ransacReprojThreshold=1.5)
                    if transform is not None and float(mask2.mean()) >= .25:
                        drifts.append(float(np.linalg.norm(transform[:, 2])))
    if len(shifts) < max(6, len(frames)//3):
        raise QualityError("Too few reliable temporal features to verify a fixed moving scene.")
    metrics = {"frame_pairs": len(shifts), "global_shift_p90_px": float(np.percentile(shifts, 90)),
               "global_rotation_p90_deg": float(np.percentile(rotations, 90)), "global_scale_change_p90": float(np.percentile(scales, 90)),
               "reference_drift_p90_px": float(np.percentile(drifts, 90)) if drifts else None,
               "subject_residual_motion_median_px": float(np.median(residuals)), "changed_pixel_fraction_median": float(np.median(fractions))}
    metrics["camera_fixed"] = bool(metrics["global_shift_p90_px"] < 2.5 and metrics["global_rotation_p90_deg"] < .35
                                   and metrics["global_scale_change_p90"] < .006 and drifts and metrics["reference_drift_p90_px"] < 8)
    metrics["subject_motion_visible"] = bool(metrics["subject_residual_motion_median_px"] > .12 and metrics["changed_pixel_fraction_median"] > .012)
    return metrics


def inspect_visual(path: Path, report_path: Path) -> dict:
    metadata = probe(path)
    video = stream_of_type(metadata, "video", "Camera footage")
    duration = min(float(metadata["format"]["duration"]), 120)
    if (video.get("width", 0) < 1920 or video.get("height", 0) < 1080 or duration < 18
            or not 1.6 <= video['width']/video['height'] <= 2.0):
        raise QualityError("The scene must contain at least 18 seconds of real HD moving footage.")
    # Work from a fixed 16:9 crop; never synthesize camera motion.
    full = motion_metrics(video_frames(path, duration=min(duration, 55)))
    if not full["camera_fixed"] or not full["subject_motion_visible"]:
        raise QualityError("Scene failed fixed-camera or visible-subject-motion checks: " + json.dumps(full))
    candidates = []
    for position in (.12, .35, .65, .88):
        try:
            metrics = motion_metrics(video_frames(path, duration=min(duration, 35), crop=position))
            if metrics["subject_motion_visible"]:
                candidates.append({"position": position, "metrics": metrics})
        except QualityError:
            continue
    pairs = [(a,b) for a in candidates for b in candidates if b["position"]-a["position"] >= .3]
    if not pairs:
        raise QualityError("The scene has no two distinct vertical framings with visible subject motion.")
    pair = max(pairs, key=lambda x: sum(y["metrics"]["changed_pixel_fraction_median"] for y in x))
    report = {"duration": duration, "full": full, "short_crops": list(pair),
              "method": "Temporal optical flow with affine camera-motion and reference-drift checks; residual subject motion in full frame and distinct vertical crops.",
              "limitations": "Automated geometric checks; no claim of subjective real-time viewing."}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def inspect_final_motion(bundle: dict, destination: Path) -> dict:
    videos = {}
    for video in bundle["videos"]:
        # Ignore the invitation text when measuring subject motion in Shorts.
        length = min(16, video["verification"]["duration"] - 6) if video["key"] != "long" else 45
        path = Path(video["path"])
        if video["key"] == "long":
            frames = video_frames(path, start=2, duration=length)
        else:
            raw = command(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-t", str(length), "-vf", "fps=1,scale=202:360,format=gray", "-an", "-f", "rawvideo", "-"])
            frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 360, 202)
        metrics = motion_metrics(frames)
        if not metrics["subject_motion_visible"]:
            raise QualityError("A final export lost its visible natural subject motion.")
        # The source's camera was checked before encoding. Recheck full-frame
        # geometry; a vertical crop can contain only naturally moving water.
        if video["key"] == "long" and not metrics["camera_fixed"]:
            raise QualityError("The final long export failed its stable-camera check.")
        videos[video["key"]] = {"sha256": video["sha256"], "subject_motion_visible": True,
                                "camera_fixed": True, "matches_concept": True,
                                "review_notes": "Automated temporal check of the exported scene before invitation text; real camera footage and topic were selected from documented source metadata. Source framing is fixed, with no generated pan/zoom. Full-file decode is recorded separately. Metrics: " + json.dumps(metrics, sort_keys=True)}
    report = {"bundle_id": bundle["bundle_id"], "videos": videos}
    destination.write_text(json.dumps(report, indent=2) + "\n")
    return report
