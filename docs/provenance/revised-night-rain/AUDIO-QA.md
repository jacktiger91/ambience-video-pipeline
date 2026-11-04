# Automated audio event checks

The cloud environment cannot provide subjective listening through the assistant.
These checks therefore support, but do not replace, the owner's listening review.
The creator's recording descriptions and saved CC0 evidence establish provenance;
an event classifier cannot establish whether a sound is an original field recording.

`audio-event-check.json` records 25 source windows around rain passages and
candidate thunder events. `full-source-audio-event-check.json` covers every
selected source passage at a one-second hop: 3,648 seconds before the three
16-second overlaps. Original source intervals were not repeated.

The official TensorFlow YAMNet TFLite model ran locally with two CPU threads,
using the Apache-2.0 LiteRT runtime. The 4.1 MB model and 21.8 MB runtime were
downloaded into `/tmp`, with TLS verification enabled and the runtime wheel
verified against its PyPI SHA-256. The GCS model MD5 was verified against the
server's `x-goog-hash` header; the model SHA-256, URLs and class-map hash are in
the reports. No paid service, training or external audio-analysis API was used.

Across the full selected passages, no frame reached a Speech or Music score of
0.25. Maximum scores were approximately 0.199 and 0.043 respectively. These
scores are model outputs, not calibrated probabilities or a guarantee that every
incidental sound is absent. RossJuterbock and lastraindrop passages strongly
matched rain, raindrops and rain on surfaces. The joedeshon recording also
matched water/stream/noise classes, illustrating the limits of classification
for continuous rain and runoff.

Five stronger rumble candidates met both a low-frequency rise detector and a
strict semantic criterion: maximum Thunder score at least 0.9 and at least ten
overlapping frames at or above 0.5. Their final mix onsets are 1270.7, 2908.3,
3377.2, 3456.7 and 3554.6 seconds. Those alone receive subtle sky glows. Peaks
within the lastraindrop passage were classified as rainfall, so no glows were
attached to those rainfall changes. Timing is automatically estimated, not
confirmed by human listening.

`audio_event_audit.py` is an optional reproducible diagnostic. It accepts an
official YAMNet TFLite model, its CSV class map and a JSON list of source paths,
start times and durations. It leaves the source stereo audio untouched; only
the analysis input is downmixed to the model's required 16 kHz mono format.

Final audio loudness, true peak, stereo and transition checks are separate
reports under `outputs/revised-night-rain/audio-audit/`. The full video decoder
checks run separately from this audio-only analysis.
