# Investigation Report: Dialogue Loss After TTS Announcements on TCL Android TV

> **⚠️ Superseded.** This report's conclusion (unfixable TCL firmware
> defect) was wrong. See
> [`investigation-dialogue-loss-resolution.md`](./investigation-dialogue-loss-resolution.md)
> for the actual root cause (an app-level audio routing issue) and the fix.
> Kept here for the investigative trail, which is still accurate up to its
> conclusion.

## Summary

While developing the **TTS Bridge** Android application, an issue was discovered where some streaming applications permanently lost dialogue after a Text-to-Speech announcement interrupted playback.

Initially, the bridge itself appeared to be responsible. However, systematic testing isolated the issue to a much narrower condition involving **multichannel (5.1) audio restoration** on the television.

The investigation strongly suggests this is **not a defect in the bridge**, but rather an interaction between Android's audio interruption mechanism and TCL's audio processing pipeline.

---

# Original Symptoms

After a TTS announcement:

* YouTube resumed normally.
* Spotify resumed normally.
* Stremio resumed normally.
* Netflix resumed with dialogue missing.
* Nuvio resumed with dialogue missing.

The missing dialogue was not temporary.

Characteristics:

* Background music remained.
* Ambient effects remained.
* Explosions remained.
* Character voices disappeared.

Even pausing and resuming playback did not restore dialogue.

Only seeking (scrubbing the playback position) restored normal audio.

---

# Initial Hypotheses

Several possible causes were investigated.

## Hypothesis 1 — Audio Focus handling

Perhaps the bridge was requesting or abandoning Audio Focus incorrectly.

Result:

Not supported by testing.

Changing focus behavior produced no meaningful difference.

---

## Hypothesis 2 — AudioAttributes

Different AudioAttributes were considered:

* USAGE_MEDIA
* USAGE_ASSISTANCE_NAVIGATION_GUIDANCE
* USAGE_ASSISTANCE_ACCESSIBILITY
* USAGE_ASSISTANCE_SONIFICATION

Some Android versions also expose announcement-specific usages.

None explained why only specific streaming apps were affected.

---

## Hypothesis 3 — Ducking vs Pausing

The bridge was tested against applications that reacted differently to Audio Focus.

Observed behaviour:

### YouTube

* pauses
* resumes correctly

### Spotify

* pauses
* resumes correctly

### Stremio

* pauses
* resumes correctly

### Netflix

* ducks audio
* dialogue disappears

Initially this suggested ducking was responsible.

However, later testing disproved this.

---

# Discovery: Nuvio

Nuvio behaved differently from Netflix.

Instead of ducking, it paused playback.

After resuming:

Dialogue was still missing.

This eliminated ducking as the root cause.

The common factor had to be elsewhere.

---

# Audio Processing Investigation

The next experiment was disabling the television's built-in audio processing.

Examples include:

* Dolby Audio
* Virtual surround
* Audio enhancements
* Manufacturer DSP

Results:

## Internal TV speakers

Audio processing ON

Netflix:

❌ Dialogue lost

Nuvio:

❌ Dialogue lost

Audio processing OFF

Netflix:

✅ Works correctly

Nuvio:

✅ Works correctly

This was the first major breakthrough.

Simply disabling the television's processing completely eliminated the issue.

---

# Optical Output Testing

The next experiment used optical (S/PDIF) audio output.

Results:

Dialogue loss returned.

This suggested the issue was not limited to the TV speakers themselves.

Instead, it appeared to follow whichever output path involved multichannel processing.

---

# Audio Track Testing

Netflix allows switching between different audio tracks.

Two tracks were tested:

* English (Stereo)
* English (5.1)

Results:

## English (Stereo)

Internal speakers

✅ Works

Optical

✅ Works

## English (5.1)

Internal speakers

❌ Dialogue lost

Optical

❌ Dialogue lost

This became the strongest piece of evidence collected during the investigation.

---

# Conclusions

The bridge itself does not appear to corrupt playback.

Instead, the issue only appears when all of the following are true:

* playback uses a multichannel audio track (5.1)
* playback is temporarily interrupted by another audio source
* playback resumes through TCL's multichannel audio pipeline

Stereo playback never exhibited the issue.

Seeking forces the decoder/output pipeline to rebuild, immediately restoring dialogue.

This strongly suggests that the interruption leaves the television's multichannel audio pipeline in an invalid state.

---

# Most Likely Technical Explanation

The exact implementation is proprietary, but the observed behaviour is consistent with the following sequence.

1. Netflix outputs 5.1 audio.
2. Dialogue is carried primarily on the centre channel.
3. The bridge temporarily acquires audio focus and plays speech.
4. Playback resumes.
5. TCL's multichannel processing fails to correctly restore the centre channel.
6. Background channels continue working normally.
7. Seeking rebuilds the decoder/output chain, restoring correct channel mapping.

This explains every observed symptom.

---

# Evidence Supporting This Theory

The following observations all support the multichannel restoration hypothesis.

✔ YouTube unaffected

✔ Spotify unaffected

✔ Stremio unaffected

✔ Netflix affected only when using 5.1

✔ Nuvio affected only when using 5.1

✔ Stereo tracks always work

✔ Disabling TV audio processing fixes the issue

✔ Seeking restores dialogue immediately

✔ The bridge itself continues to function correctly

---

# Possible Root Cause

The evidence points toward a firmware or platform issue involving:

* TCL audio processing
* Dolby processing
* multichannel output restoration
* Android Audio Focus interaction

rather than a defect in the bridge application.

Whether the bug ultimately resides in TCL firmware, Dolby processing, or the Android audio stack cannot be determined without vendor source code.

---

# Potential Workarounds

## For users

* Disable the television's audio processing.
* Use stereo audio tracks instead of 5.1 where acceptable.

Both eliminate the issue completely based on current testing.

---

## For the bridge

The bridge cannot directly fix firmware behaviour.

However, it can offer compatibility options, such as:

* alternative Audio Focus strategies
* different AudioAttributes
* configurable playback modes
* optional "compatibility mode" profiles

Although these may avoid triggering problematic firmware paths on some devices.

---

# Future Investigation

The following experiments may provide additional insight.

* Test HDMI ARC/eARC output.
* Test Bluetooth audio devices.
* Test external AV receivers.
* Test additional streaming applications.
* Compare behaviour on other Android TV manufacturers.
* Compare behaviour across different TCL firmware versions.
* Test additional AudioAttributes combinations.
* Capture logcat output during interruption and restoration.

---

# Final Assessment

The investigation evolved significantly.

The initial assumption was that the bridge was interfering with playback.

Through controlled testing, each hypothesis was eliminated until a clear pattern emerged.

The current evidence indicates that the bridge is merely exposing an existing weakness in the television's handling of interrupted multichannel playback.

Rather than introducing the fault, the bridge consistently reproduces an issue already present within the multichannel audio restoration pipeline.

As a result, further bridge development should focus on compatibility strategies rather than attempting to "repair" playback, since the observed behaviour appears to originate below the application's level.

Working hypothesis: Transient TTS announcements expose a firmware defect in the restoration of multichannel (5.1) audio on TCL Android TVs. All tested stereo playback paths recover correctly, while tested multichannel paths exhibit loss of the center/dialogue channel until the decoder is reinitialized (e.g., by seeking). The bridge itself appears to function correctly; it merely triggers the transition that exposes the underlying issue.
