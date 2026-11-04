#!/usr/bin/env python3
"""Optional local YAMNet event checks; classifications are not listening approval.

This does not download or install dependencies. Supply an official YAMNet
TFLite model, its class map, and a Python environment containing LiteRT + NumPy.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--class-map", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True,
                        help="JSON list of {path, start, duration, id} windows")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hop", type=float, default=0.48)
    args = parser.parse_args()
    if args.hop <= 0:
        parser.error("--hop must be positive")
    from ai_edge_litert.interpreter import Interpreter
    interpreter = Interpreter(model_path=str(args.model), num_threads=2)
    interpreter.allocate_tensors()
    inp = interpreter.get_input_details()[0]
    out = interpreter.get_output_details()[0]
    length = int(np.prod(inp["shape"]))
    if length != 15600 or inp["dtype"] != np.float32:
        raise ValueError("Expected official YAMNet float32 15600-sample input")
    with args.class_map.open() as handle:
        labels = [row["display_name"] for row in csv.DictReader(handle)]
    with zipfile.ZipFile(args.model) as archive:
        embedded_labels = archive.read("yamnet_label_list.txt").decode().splitlines()
    if labels != embedded_labels:
        raise ValueError("Class map does not match this model's embedded labels")
    interest = ["Rain", "Raindrop", "Rain on surface", "Water", "Thunder",
                "Thunderstorm", "Speech", "Music", "Wind", "Noise", "Ocean",
                "Waves, surf", "Stream", "Splash, splatter",
                "Wind noise (microphone)", "Screaming", "Siren",
                "Gunshot, gunfire", "Fireworks", "Vehicle",
                "Vehicle horn, car horn, honking", "Aircraft", "Dog", "Bird",
                "Boat, Water vehicle", "Sailboat, sailing ship",
                "Rowboat, canoe, kayak", "Motorboat, speedboat", "Engine",
                "Gurgling", "Slosh"]
    incidental = ["Wind noise (microphone)", "Screaming", "Siren",
                  "Gunshot, gunfire", "Fireworks", "Vehicle",
                  "Vehicle horn, car horn, honking", "Aircraft", "Dog", "Bird",
                  "Motorboat, speedboat", "Engine"]
    ids = {label: labels.index(label) for label in interest}
    records = []
    for request in json.loads(args.requests.read_text()):
        path = Path(request["path"])
        start = float(request.get("start", 0))
        duration = float(request["duration"])
        if start < 0 or duration <= 0 or not path.is_file():
            raise ValueError(f"Invalid audio request: {request}")
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
             "-t", str(duration), "-vn", "-ar", "16000", "-ac", "1",
             "-f", "f32le", "-"], check=True, stdout=subprocess.PIPE).stdout
        samples = np.frombuffer(decoded, dtype="<f4")
        if len(samples) < length:
            raise ValueError(f"Less than one YAMNet frame in {path}")
        scores = []
        frame_times = []
        for offset in range(0, len(samples) - length + 1, round(args.hop * 16000)):
            interpreter.set_tensor(inp["index"], samples[offset:offset + length].reshape(inp["shape"]))
            interpreter.invoke()
            scores.append(interpreter.get_tensor(out["index"]).reshape(-1))
            frame_times.append(start + offset / 16000)
        matrix = np.array(scores)
        means = matrix.mean(axis=0)
        record = {**request, "analysed_seconds": len(samples) / 16000,
                  "frames": len(scores), "top_mean_labels": [
                      {"label": labels[i], "score": round(float(means[i]), 5)}
                      for i in np.argsort(means)[-8:][::-1]],
                  "events": {label: {
                      "mean_score": round(float(means[i]), 5),
                      "max_score": round(float(matrix[:, i].max()), 5),
                      "max_frame_start_seconds": round(frame_times[int(matrix[:, i].argmax())], 3),
                      "frames_at_least_0_25": int(np.sum(matrix[:, i] >= 0.25)),
                      "frames_at_least_0_5": int(np.sum(matrix[:, i] >= 0.5))
                  } for label, i in ids.items()},
                  "thunder_frames": [
                      {"source_frame_start": round(frame_times[j], 3),
                       "thunder": round(float(row[ids["Thunder"]]), 4),
                       "thunderstorm": round(float(row[ids["Thunderstorm"]]), 4)}
                      for j, row in enumerate(matrix)
                      if row[ids["Thunder"]] >= 0.25 or row[ids["Thunderstorm"]] >= 0.5],
                  "speech_music_candidates": [
                      {"source_frame_start": round(frame_times[j], 3),
                       "speech": round(float(row[ids["Speech"]]), 4),
                       "music": round(float(row[ids["Music"]]), 4),
                       "top_labels": [labels[i] for i in np.argsort(row)[-3:][::-1]]}
                      for j, row in enumerate(matrix)
                      if row[ids["Speech"]] >= 0.25 or row[ids["Music"]] >= 0.25],
                  "incidental_event_candidates": [
                      {"source_frame_start": round(frame_times[j], 3),
                       "events": {label: round(float(row[ids[label]]), 4)
                                  for label in incidental if row[ids[label]] >= 0.5},
                       "top_labels": [labels[i] for i in np.argsort(row)[-3:][::-1]]}
                      for j, row in enumerate(matrix)
                      if any(row[ids[label]] >= 0.5 for label in incidental)]}
        records.append(record)
        print(json.dumps({"id": request.get("id", path.name),
                          "top": record["top_mean_labels"][:3],
                          "max_thunder": record["events"]["Thunder"]["max_score"],
                          "max_waves": record["events"]["Waves, surf"]["max_score"],
                          "max_speech": record["events"]["Speech"]["max_score"],
                          "max_music": record["events"]["Music"]["max_score"]}), flush=True)
    report = {
        "method": "Official YAMNet TFLite AudioSet event classification, 16 kHz mono analysis only; original stereo media untouched.",
        "limitations": "Automated scores are not calibrated probabilities, do not prove natural origin or rights, and do not replace human listening. No subjective listening was possible in this environment.",
        "model_url": "https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/audio_classification/android/lite-model_yamnet_classification_tflite_1.tflite",
        "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
        "class_map_sha256": hashlib.sha256(args.class_map.read_bytes()).hexdigest(),
        "class_map_url": "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv",
        "model_code_license": "Apache-2.0; https://github.com/tensorflow/models/blob/master/LICENSE",
        "model_embedded_license": "Apache License. Version 2.0 http://www.apache.org/licenses/LICENSE-2.0.",
        "embedded_521_labels_match_official_class_map": True,
        "runtime": "ai-edge-litert 2.3.0 (Apache-2.0), temporary install",
        "runtime_linux_cp312_wheel_sha256": "dc9f56c23e0dcf182b6c35f23fbb102ed048296588882b806d5eff7654c04c88",
        "hop_seconds": args.hop, "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
