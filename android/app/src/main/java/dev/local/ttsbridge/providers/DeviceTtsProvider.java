package dev.local.ttsbridge.providers;

import android.content.Context;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Log;

import java.util.Locale;
import java.util.UUID;

import dev.local.ttsbridge.core.Announcement;

/** On-device synthesis (offline, whatever voice is installed on the box). */
public class DeviceTtsProvider implements AnnouncementProvider, TextToSpeech.OnInitListener {

    private static final String TAG = "TtsBridge/DeviceTts";

    private final TextToSpeech tts;
    private volatile boolean ready = false;
    private Runnable pendingWhenReady;

    // tts.stop() halts audio immediately but does NOT reliably fire onDone/
    // onError for the interrupted utterance, so the engine's CountDownLatch
    // would otherwise sit blocked until the full timeout. We track the
    // in-flight listener ourselves and notify it directly from stop().
    private volatile Listener activeListener;
    private volatile Announcement activeAnnouncement;
    private final java.util.concurrent.atomic.AtomicBoolean settled = new java.util.concurrent.atomic.AtomicBoolean(true);

    public DeviceTtsProvider(Context appContext) {
        tts = new TextToSpeech(appContext, this);
    }

    @Override
    public void onInit(int status) {
        ready = status == TextToSpeech.SUCCESS;
        if (ready) {
            tts.setLanguage(Locale.getDefault());
        } else {
            Log.e(TAG, "TTS engine init failed, status=" + status);
        }
        if (pendingWhenReady != null) {
            Runnable r = pendingWhenReady;
            pendingWhenReady = null;
            r.run();
        }
    }

    @Override
    public boolean supports(Announcement a) {
        return a.url == null || a.url.trim().isEmpty();
    }

    @Override
    public void play(Announcement a, Listener listener) {
        if (!ready) {
            // engine still warming up (e.g. right after boot) - wait for onInit
            pendingWhenReady = () -> playNow(a, listener);
            return;
        }
        playNow(a, listener);
    }

    private void playNow(Announcement a, Listener listener) {
        if (!ready) {
            listener.onError(a, "tts_engine_unavailable");
            return;
        }
        activeListener = listener;
        activeAnnouncement = a;
        settled.set(false);

        String utteranceId = UUID.randomUUID().toString();
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { listener.onStarted(a); }
            @Override public void onDone(String id) { finishOnce(() -> listener.onFinished(a)); }
            @Override public void onError(String id) { finishOnce(() -> listener.onError(a, "tts_synthesis_error")); }
        });
        Bundle params = new Bundle();
        int result = tts.speak(a.text, TextToSpeech.QUEUE_FLUSH, params, utteranceId);
        if (result != TextToSpeech.SUCCESS) {
            finishOnce(() -> listener.onError(a, "tts_speak_rejected"));
        }
    }

    /** Guards against both the system callback AND our own stop()-triggered
     *  notification firing for the same utterance. */
    private void finishOnce(Runnable notify) {
        if (settled.compareAndSet(false, true)) {
            notify.run();
        }
    }

    @Override
    public void stop() {
        if (tts != null) tts.stop();
        Listener l = activeListener;
        Announcement a = activeAnnouncement;
        if (l != null && a != null) {
            finishOnce(() -> l.onError(a, "interrupted"));
        }
    }

    @Override
    public void release() {
        if (tts != null) {
            tts.stop();
            tts.shutdown();
        }
    }
}
