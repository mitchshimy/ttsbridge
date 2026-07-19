package dev.local.ttsbridge.providers;

import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.util.Log;

import dev.local.ttsbridge.core.Announcement;

/**
 * Plays a pre-rendered clip - typically a URL from HA's own tts.piper /
 * tts.google_translate / homeway_sage engines, so the voice matches whatever
 * plays on your other media_players. Also handles local file paths.
 */
public class UrlAudioProvider implements AnnouncementProvider {

    private static final String TAG = "TtsBridge/UrlAudio";
    private MediaPlayer mediaPlayer;

    // mediaPlayer.stop() does NOT fire onCompletion/onError, so - same as
    // DeviceTtsProvider - we track the in-flight listener ourselves and
    // notify it directly from stop(), guarded against double-firing.
    private volatile Listener activeListener;
    private volatile Announcement activeAnnouncement;
    private final java.util.concurrent.atomic.AtomicBoolean settled = new java.util.concurrent.atomic.AtomicBoolean(true);

    @Override
    public boolean supports(Announcement a) {
        return a.url != null && !a.url.trim().isEmpty();
    }

    @Override
    public void play(Announcement a, Listener listener) {
        activeListener = listener;
        activeAnnouncement = a;
        settled.set(false);
        try {
            if (mediaPlayer != null) {
                mediaPlayer.release();
            }
            mediaPlayer = new MediaPlayer();
            mediaPlayer.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mediaPlayer.setDataSource(a.url);

            mediaPlayer.setOnPreparedListener(mp -> {
                listener.onStarted(a);
                mp.start();
            });
            mediaPlayer.setOnCompletionListener(mp -> {
                finishOnce(() -> listener.onFinished(a));
                releaseInternal();
            });
            mediaPlayer.setOnErrorListener((mp, what, extra) -> {
                Log.e(TAG, "MediaPlayer error what=" + what + " extra=" + extra);
                finishOnce(() -> listener.onError(a, "media_player_error_" + what));
                releaseInternal();
                return true;
            });
            mediaPlayer.prepareAsync();
        } catch (Exception e) {
            Log.e(TAG, "playUrl failed", e);
            finishOnce(() -> listener.onError(a, "media_player_exception: " + e.getMessage()));
        }
    }

    /** Guards against both the system callback AND our own stop()-triggered
     *  notification firing for the same clip. */
    private void finishOnce(Runnable notify) {
        if (settled.compareAndSet(false, true)) {
            notify.run();
        }
    }

    @Override
    public void stop() {
        if (mediaPlayer != null) {
            try {
                if (mediaPlayer.isPlaying()) mediaPlayer.stop();
            } catch (IllegalStateException ignored) {
            }
        }
        Listener l = activeListener;
        Announcement a = activeAnnouncement;
        if (l != null && a != null) {
            finishOnce(() -> l.onError(a, "interrupted"));
        }
        releaseInternal();
    }

    private void releaseInternal() {
        if (mediaPlayer != null) {
            mediaPlayer.release();
            mediaPlayer = null;
        }
    }

    @Override
    public void release() {
        releaseInternal();
    }
}
