package dev.local.ttsbridge.providers;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
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
    private volatile HttpURLConnection activeConnection;
    private volatile boolean cancelled;

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
        cancelled = false;

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
        File tempFile;
        try {
            tempFile = downloadToTempFile(a);
        } catch (Exception e) {
            if (cancelled) return;
            // The network fetch itself failed - the url is unreachable (or
            // too slow). Do NOT retry it via MediaPlayer: that has no
            // comparably fast, configurable timeout for network sources and
            // ends up hanging on Android's own much longer default (this is
            // exactly what turned a 3s fail into a 15-20s one in testing).
            // Report the failure now so AnnouncementEngine's device-TTS
            // fallback can take over quickly instead.
            Log.w(TAG, "cache-miss download failed for key=" + a.cacheKey + " (network error, not retrying same url via MediaPlayer)", e);
            finishOnce(() -> listener.onError(a, "download_failed: " + e.getMessage()));
            return;
        }

        if (cancelled) {
            //noinspection ResultOfMethodCallIgnored
            tempFile.delete();
            return;
        }

        File committed;
        try {
            committed = speechCache.put(a.cacheKey, tempFile);
        } catch (IOException e) {
            // Download succeeded (url is confirmed reachable) but committing
            // to the cache failed for some local reason (disk full, etc) -
            // here a direct-stream retry of the same url is actually
            // meaningful, since we just proved it's reachable.
            Log.w(TAG, "cache write failed for key=" + a.cacheKey + ", streaming directly instead", e);
            //noinspection ResultOfMethodCallIgnored
            tempFile.delete();
            playDataSource(a.url, a, listener);
            return;
        }
        //noinspection ResultOfMethodCallIgnored
        tempFile.delete();

        if (cancelled) return;
        playDataSource(committed.getAbsolutePath(), a, listener);
    }

    /** Fetches a.url into a fresh temp file. Any thrown exception here means the network fetch itself failed. */
    private File downloadToTempFile(Announcement a) throws IOException {
        File tempFile = File.createTempFile("ttsbridge_urlcache_", ".audio", null);
        HttpURLConnection conn = (HttpURLConnection) new URL(a.url).openConnection();
        // 3s, not the original 8s: this is the gap between "media gets
        // ducked/paused" and "we notice HA is unreachable and can fall back
        // to something audible" (see AnnouncementEngine's url-failure ->
        // device-TTS fallback). A dead connection should fail fast; a
        // merely-slow-but-alive one still has 15s of read timeout once
        // bytes start flowing.
        conn.setConnectTimeout(3000);
        conn.setReadTimeout(15000);
        activeConnection = conn;
        try {
            if (cancelled) throw new IOException("cancelled");
            try (InputStream is = conn.getInputStream(); FileOutputStream fos = new FileOutputStream(tempFile)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = is.read(buf)) != -1) {
                    if (cancelled) throw new IOException("cancelled");
                    fos.write(buf, 0, n);
                }
            }
            return tempFile;
        } finally {
            conn.disconnect();
            activeConnection = null;
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
        cancelled = true;
        HttpURLConnection conn = activeConnection;
        if (conn != null) {
            // Safe to call from another thread; unblocks any in-progress
            // connect()/read() in downloadThenPlay so it notices `cancelled`
            // promptly instead of running out its full timeout unsupervised.
            conn.disconnect();
        }
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