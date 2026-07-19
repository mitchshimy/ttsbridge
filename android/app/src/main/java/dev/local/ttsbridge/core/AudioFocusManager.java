package dev.local.ttsbridge.core;

import android.content.Context;
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
 */
public class AudioFocusManager {

    private static final String TAG = "TtsBridge/Focus";

    private final AudioManager audioManager;
    private AudioFocusRequest focusRequest; // API 26+
    private Integer savedStreamVolume; // non-null only while we've manually lowered it

    private final AudioManager.OnAudioFocusChangeListener focusListener = change ->
            Log.d(TAG, "onAudioFocusChange=" + change);

    public AudioFocusManager(Context context) {
        this.audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
    }

    public void request(boolean duck) {
        int gainType = duck
                ? AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK
                : AudioManager.AUDIOFOCUS_GAIN_TRANSIENT;

        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
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

    public void abandon() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && focusRequest != null) {
            audioManager.abandonAudioFocusRequest(focusRequest);
        } else {
            //noinspection deprecation
            audioManager.abandonAudioFocus(focusListener);
        }
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
