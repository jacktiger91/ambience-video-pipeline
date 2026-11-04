# Quiet Shore audio checks

The assistant cannot subjectively listen to audio in this environment. These
checks support the owner's listening review; they do not certify that a recording
will sound pleasant to every listener. Source descriptions and saved licences,
not machine classification, establish the field-recording provenance and rights.

The cached official TensorFlow YAMNet model ran locally with the LiteRT runtime
and two CPU threads. Both carry Apache-2.0 licences. No paid audio-analysis API,
new model download or synthetic rain source was used. The reports retain the
official model URL, SHA-256 and class-map verification. Analysis alone uses
16 kHz mono; source and delivery audio remain stereo.

`candidate-audio-event-check.json` and
`additional-candidate-audio-event-check.json` cover four complete candidate
recordings at a one-second hop. The De Haan recording strongly matched ocean
and waves. Blackpool Sands was a rumblier, less clearly classified alternative.
Whitby matched sheltered water/boat classes and had probable speech later in the
recording. An early impact in De Haan and an isolated impact in Dunwich were also
flagged. Those passages were excluded before rendering.

`selected-source-audio-event-check.json` checks the exact selected passages:

| Recording | Source seconds | Length |
| --- | --- | --- |
| Whitby, `aporee_37802_43261` | 10–1340 | 1330 s |
| De Haan, `aporee_41944_47808` | 14–1650 | 1636 s |
| Dunwich, `aporee_59218_67955` | 10–510 | 500 s |
| Whitby, `aporee_37802_43261` | 1380–1562 | 182 s |

These are distinct intervals totalling 3648 seconds. Three 16-second crossfades
produce 3600 seconds without repeating a passage. No selected frame reached a
Speech or Music score of 0.25; maxima were 0.19922 and 0.01953 respectively.
Engine scores remained at or below 0.01172. Whitby's frequent Boat labels were
predominantly Sailboat/Rowboat, consistent with the creator's description of
water gurgling under a slipway; they do not establish that an engine is present.
Scores are not calibrated probabilities or guarantees that every incidental
sound is absent.

`outputs/quiet-shore/audio-audit/source-stereo-check.json` records three stereo
windows per source, with differing left and right signals. Recommended Shorts
use Whitby source 650–675 seconds and De Haan source 600–625 seconds, separately
checked at a 0.48-second hop. Final mix transitions, delivery AAC loudness,
true-peak headroom and stereo checks are recorded separately in the audio-audit
folder once the exports are complete.

## Final delivery measurements

All three delivery files passed audio decode, stereo-channel and true-peak
headroom checks. The three final crossfade windows contained no detected silent
gaps or nonfinite samples, and continued to classify as ocean/waves/water.

| File | Integrated loudness | True peak | Loudness range |
| --- | --- | --- | --- |
| Full hour | −22.2 LUFS | −3.6 dBTP | 8.6 LU |
| Short 1 | −22.8 LUFS | −4.2 dBTP | 5.8 LU |
| Short 2 | −21.6 LUFS | −4.4 dBTP | 7.9 LU |

The final `export-audio-check.json` binds measurements to the delivery paths and
SHA-256 values. Playback volume and perceived comfort still require the owner’s
listening review.
