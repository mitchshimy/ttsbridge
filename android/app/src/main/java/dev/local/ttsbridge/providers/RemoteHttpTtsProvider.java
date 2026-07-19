package dev.local.ttsbridge.providers;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.util.Log;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;

import dev.local.ttsbridge.core.Announcement;

/**
 * Calls a self-hosted HTTP TTS server directly - built for Piper, but works
 * for any similarly simple server since the exact wire format varies by
 * deployment (there's no single standard "Piper HTTP API"):
 *
 *  - Request: either a raw text/plain POST body, or JSON {"text": "..."}
 *    (jsonRequest flag), since different Piper HTTP wrappers expect one or
 *    the other.
 *  - Response: auto-detected from Content-Type. "audio/*" is read as raw
 *    bytes and played from a temp file. Anything else is parsed as JSON
 *    and expected to contain a "url" field pointing to fetchable audio
 *    (matches how HA's own tts.speak responses are typically shaped).
 *
 * Runs entirely independently of Home Assistant - the point is this still
 * works to announce something even if HA or the network to it is down.
 */
public class RemoteHttpTtsProvider implements AnnouncementProvider {

    private static final String TAG = "TtsBridge/RemoteTts";

    private final Context appContext;
    private final String engineId;
    private final String baseUrl;
    private final boolean jsonRequest;

    private volatile Listener activeListener;
    private volatile Announcement activeAnnouncement;
    private volatile HttpURLConnection activeConnection;
    private volatile MediaPlayer mediaPlayer;
    private volatile File tempFile;
    private final AtomicBoolean settled = new AtomicBoolean(true);

    public RemoteHttpTtsProvider(Context appContext, String engineId, String baseUrl, boolean jsonRequest) {
        this.appContext = appContext.getApplicationContext();
        this.engineId = engineId;
        this.baseUrl = baseUrl;
        this.jsonRequest = jsonRequest;
    }

    @Override
    public boolean supports(Announcement a) {
        return a.text != null && !a.text.trim().isEmpty();
    }

    @Override
    public void play(Announcement a, Listener listener) {
        activeListener = listener;
        activeAnnouncement = a;
        settled.set(false);
        new Thread(() -> synthesizeAndPlay(a, listener), "RemoteTts-" + engineId).start();
    }

    private void synthesizeAndPlay(Announcement a, Listener listener) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(baseUrl).openConnection();
            activeConnection = conn;
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(15000);

            byte[] requestBody;
            if (jsonRequest) {
                conn.setRequestProperty("Content-Type", "application/json");
                requestBody = new JSONObject().put("text", a.text).toString().getBytes(StandardCharsets.UTF_8);
            } else {
                conn.setRequestProperty("Content-Type", "text/plain; charset=utf-8");
                requestBody = a.text.getBytes(StandardCharsets.UTF_8);
            }
            try (OutputStream os = conn.getOutputStream()) {
                os.write(requestBody);
            }

            int status = conn.getResponseCode();
            if (status < 200 || status >= 300) {
                finishOnce(() -> listener.onError(a, engineId + "_http_" + status));
                return;
            }

            String contentType = conn.getContentType();
            String dataSource;
            if (contentType != null && contentType.toLowerCase().startsWith("audio/")) {
                tempFile = File.createTempFile("ttsbridge_" + engineId + "_", ".audio", appContext.getCacheDir());
                try (InputStream is = conn.getInputStream(); FileOutputStream fos = new FileOutputStream(tempFile)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = is.read(buf)) != -1) fos.write(buf, 0, n);
                }
                dataSource = tempFile.getAbsolutePath();
            } else {
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                try (InputStream is = conn.getInputStream()) {
                    byte[] buf = new byte[4096];
                    int n;
                    while ((n = is.read(buf)) != -1) baos.write(buf, 0, n);
                }
                JSONObject resp = new JSONObject(baos.toString("UTF-8"));
                String remoteUrl = resp.optString("url", null);
                if (remoteUrl == null || remoteUrl.trim().isEmpty()) {
                    finishOnce(() -> listener.onError(a, engineId + "_response_had_no_audio_or_url"));
                    return;
                }
                dataSource = remoteUrl;
            }

            playAudio(dataSource, a, listener);
        } catch (Exception e) {
            Log.w(TAG, engineId + " synthesis failed", e);
            finishOnce(() -> listener.onError(a, engineId + "_exception: " + e.getMessage()));
        } finally {
            if (conn != null) conn.disconnect();
            activeConnection = null;
        }
    }

    private void playAudio(String dataSource, Announcement a, Listener listener) {
        try {
            MediaPlayer mp = new MediaPlayer();
            mediaPlayer = mp;
            mp.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mp.setDataSource(dataSource);
            mp.setOnPreparedListener(p -> {
                listener.onStarted(a);
                p.start();
            });
            mp.setOnCompletionListener(p -> {
                finishOnce(() -> listener.onFinished(a));
                cleanup();
            });
            mp.setOnErrorListener((p, what, extra) -> {
                finishOnce(() -> listener.onError(a, engineId + "_playback_error_" + what));
                cleanup();
                return true;
            });
            mp.prepareAsync();
        } catch (Exception e) {
            finishOnce(() -> listener.onError(a, engineId + "_playback_exception: " + e.getMessage()));
            cleanup();
        }
    }

    private void cleanup() {
        if (mediaPlayer != null) {
            mediaPlayer.release();
            mediaPlayer = null;
        }
        if (tempFile != null) {
            //noinspection ResultOfMethodCallIgnored
            tempFile.delete();
            tempFile = null;
        }
    }

    private void finishOnce(Runnable notify) {
        if (settled.compareAndSet(false, true)) {
            notify.run();
        }
    }

    @Override
    public void stop() {
        // Interrupt an in-flight HTTP fetch, if that's where we are.
        HttpURLConnection conn = activeConnection;
        if (conn != null) {
            conn.disconnect();
        }
        // Or interrupt in-flight playback, if we got that far already.
        MediaPlayer mp = mediaPlayer;
        if (mp != null) {
            try {
                if (mp.isPlaying()) mp.stop();
            } catch (IllegalStateException ignored) {
            }
        }
        Listener l = activeListener;
        Announcement a = activeAnnouncement;
        if (l != null && a != null) {
            finishOnce(() -> l.onError(a, "interrupted"));
        }
        cleanup();
    }

    @Override
    public void release() {
        cleanup();
    }
}
