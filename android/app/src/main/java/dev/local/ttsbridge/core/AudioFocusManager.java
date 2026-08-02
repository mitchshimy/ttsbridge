package dev.local.ttsbridge.core;

import android.content.Context;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.os.Build;
import android.util.Log;

/**
 * Wraps AudioFocus so the engine can ask for either a hard TRANSIENT gain
 * (other audio pauses) or MAY_DUCK (other audio just gets quieter), and
 * restores whatever the stream volume was before we touched it - useful on
 * pre-O devices or apps that don't duck automatically and need us to lower
 * the stream volume ourselves during the announcement.
 *
 * Also supports a "manual_duck_only" strategy that skips the AudioFocus API
 * entirely - see requestManualOnly() for why this exists.
 */
public class AudioFocusManager {

    public static final String STRATEGY_SYSTEM = "system";
    public static final String STRATEGY_MANUAL_DUCK_ONLY = "manual_duck_only";

    private static final String TAG = "TtsBridge/Focus";
    private static final String PREFS_NAME = "ttsbridge_prefs";
    private static final String PREF_STRATEGY = "audio_focus_strategy";

    private final AudioManager audioManager;
    private final SharedPreferences prefs;
    private AudioFocusRequest focusRequest; // API 26+
    private Integer savedStreamVolume; // non-null only while we've manually lowered it
    private volatile String strategy;

    private final AudioManager.OnAudioFocusChangeListener focusListener = change ->
            Log.d(TAG, "onAudioFocusChange=" + change);

    public AudioFocusManager(Context context) {
        this.audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        this.prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        this.strategy = prefs.getString(PREF_STRATEGY, STRATEGY_SYSTEM);
    }

    public String getStrategy() {
        return strategy;
    }

    public void setStrategy(String newStrategy) {
        if (!STRATEGY_SYSTEM.equals(newStrategy) && !STRATEGY_MANUAL_DUCK_ONLY.equals(newStrategy)) {
            throw new IllegalArgumentException("Unknown strategy: " + newStrategy);
        }
        this.strategy = newStrategy;
        prefs.edit().putString(PREF_STRATEGY, newStrategy).apply();
        Log.i(TAG, "Audio focus strategy set to " + newStrategy);
    }

    public void request(boolean duck) {
        if (STRATEGY_MANUAL_DUCK_ONLY.equals(strategy)) {
            requestManualOnly();
            return;
        }
        requestViaSystemFocus(duck);
    }

    private void requestViaSystemFocus(boolean duck) {
        int gainType = duck
                ? AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK
                : AudioManager.AUDIOFOCUS_GAIN_TRANSIENT;

        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            focusRequest = new AudioFocusRequest.Builder(gainType)
                    .setAudioAttributes(attrs)
                    .setOnAudioFocusChangeListener(focusListener)
                    .build();
            int result = audioManager.requestAudioFocus(focusRequest);
            Log.d(TAG, "requestAudioFocus(duck=" + duck + ") result=" + result);
        } else {
            //noinspection deprecation
            int result = audioManager.requestAudioFocus(focusListener, AudioManager.STREAM_MUSIC, gainType);
            Log.d(TAG, "requestAudioFocus legacy(duck=" + duck + ") result=" + result);
        }

        // Belt-and-braces: on devices/apps that don't honor MAY_DUCK gracefully,
        // manually pull the music stream down a bit and remember to restore it.
        if (duck) {
            int max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            int current = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
            int duckedLevel = Math.max((int) (max * 0.3), 0);
            if (current > duckedLevel) {
                savedStreamVolume = current;
                audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, duckedLevel, 0);
            }
        }
    }

    /**
     * Never calls requestAudioFocus/abandonAudioFocus at all - other apps
     * never receive ANY AudioFocus notification (no duck, no pause signal).
     * Just directly dips the shared music stream volume and restores it
     * afterward via abandon().
     *
     * Exists to test/work around a suspected TCL firmware bug where some
     * apps' multichannel (5.1) audio pipelines fail to correctly restore
     * the center/dialogue channel after ANY AudioFocus interruption -
     * ducking or full pause, it didn't matter in testing (a properly
     * pausing app exhibited the same failure as a properly ducking one).
     * If THIS mode avoids the bug, it confirms the trigger is specifically
     * the focus notification prompting the other app's own resume logic,
     * not merely concurrent audio hitting the shared output. If the bug
     * still happens even here, it points at a hardware/mixer-level issue
     * that no app-level focus strategy can work around.
     */
    private void requestManualOnly() {
        int max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        int current = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
        int duckedLevel = Math.max((int) (max * 0.3), 0);
        if (current > duckedLevel) {
            savedStreamVolume = current;
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, duckedLevel, 0);
        }
    }

    public void abandon() {
        if (STRATEGY_MANUAL_DUCK_ONLY.equals(strategy)) {
            restoreVolumeIfNeeded();
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && focusRequest != null) {
            audioManager.abandonAudioFocusRequest(focusRequest);
        } else {
            //noinspection deprecation
            audioManager.abandonAudioFocus(focusListener);
        }
        restoreVolumeIfNeeded();
    }

    private void restoreVolumeIfNeeded() {
        if (savedStreamVolume != null) {
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, savedStreamVolume, 0);
            savedStreamVolume = null;
        }
    }

    public int getVolumePercent() {
        int max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        int current = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
        return max == 0 ? 0 : Math.round(100f * current / max);
    }

    public void setVolumePercent(int percent) {
        int p = Math.max(0, Math.min(100, percent));
        int max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        int level = Math.round(max * p / 100f);
        audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, level, 0);
    }
}