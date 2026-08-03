package dev.local.ttsbridge.providers;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

import dev.local.ttsbridge.core.Announcement;
import dev.local.ttsbridge.core.SpeechCache;

/**
 * Plays a pre-rendered clip - typically a URL from HA's own tts.piper /
 * tts.google_translate / homeway_sage engines, so the voice matches whatever
 * plays on your other media_players. Also handles local file paths.
 *
 * When Announcement.cacheKey is set, checks/populates a persistent local
 * SpeechCache first (see its class doc for why the key isn't just `url`
 * itself). No cacheKey -> identical to the pre-caching behavior: stream
 * straight from `url`, no local copy kept.
 */
public class UrlAudioProvider implements AnnouncementProvider {

    private static final String TAG = "TtsBridge/UrlAudio";
    private final SpeechCache speechCache;
    private MediaPlayer mediaPlayer;

    // mediaPlayer.stop() does NOT fire onCompletion/onError, so - same as
    // DeviceTtsProvider - we track the in-flight listener ourselves and
    // notify it directly from stop(), guarded against double-firing.
    private volatile Listener activeListener;
    private volatile Announcement activeAnnouncement;
    private final java.util.concurrent.atomic.AtomicBoolean settled = new java.util.concurrent.atomic.AtomicBoolean(true);

    public UrlAudioProvider(Context appContext) {
        this.speechCache = new SpeechCache(appContext.getApplicationContext());
    }

    @Override
    public boolean supports(Announcement a) {
        return a.url != null && !a.url.trim().isEmpty();
    }

    @Override
    public void play(Announcement a, Listener listener) {
        activeListener = listener;
        activeAnnouncement = a;
        settled.set(false);

        if (a.cacheKey == null || a.cacheKey.trim().isEmpty()) {
            playDataSource(a.url, a, listener);
            return;
        }

        File cached = speechCache.get(a.cacheKey);
        if (cached != null) {
            Log.d(TAG, "cache hit for key=" + a.cacheKey);
            playDataSource(cached.getAbsolutePath(), a, listener);
            return;
        }

        // Cache miss: download first (rather than stream) so we have bytes
        // to commit to the cache. Runs on its own thread - play()/supports()
        // contract elsewhere in this codebase assumes non-blocking calls.
        new Thread(() -> downloadThenPlay(a, listener), "UrlAudio-cache-fetch").start();
    }

    private void downloadThenPlay(Announcement a, Listener listener) {
        File tempFile = null;
        try {
            HttpURLConnection conn = (HttpURLConnection) new URL(a.url).openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(15000);
            try {
                tempFile = File.createTempFile("ttsbridge_urlcache_", ".audio", null);
                try (InputStream is = conn.getInputStream(); FileOutputStream fos = new FileOutputStream(tempFile)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = is.read(buf)) != -1) fos.write(buf, 0, n);
                }
            } finally {
                conn.disconnect();
            }

            File committed = speechCache.put(a.cacheKey, tempFile);
            playDataSource(committed.getAbsolutePath(), a, listener);
        } catch (Exception e) {
            Log.w(TAG, "cache-miss download failed for key=" + a.cacheKey + ", falling back to direct stream", e);
            // Losing the caching benefit for this one clip beats losing the
            // announcement entirely over what might be a transient network blip.
            playDataSource(a.url, a, listener);
        } finally {
            if (tempFile != null) {
                //noinspection ResultOfMethodCallIgnored
                tempFile.delete();
            }
        }
    }

    private void playDataSource(String dataSource, Announcement a, Listener listener) {
        try {
            if (mediaPlayer != null) {
                mediaPlayer.release();
            }
            mediaPlayer = new MediaPlayer();
            mediaPlayer.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mediaPlayer.setDataSource(dataSource);

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
            Log.e(TAG, "playDataSource failed", e);
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