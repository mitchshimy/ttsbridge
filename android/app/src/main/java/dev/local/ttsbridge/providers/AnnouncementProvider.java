package dev.local.ttsbridge.providers;

import dev.local.ttsbridge.core.Announcement;

/**
 * One playback backend (device TTS, a rendered-audio URL, a local file, ...).
 * The engine picks a provider per-announcement and never touches Android
 * media APIs directly, so adding e.g. a GoogleProvider or LocalFileProvider
 * later is a new class, not a rewrite.
 */
public interface AnnouncementProvider {

    interface Listener {
        void onStarted(Announcement a);
        void onFinished(Announcement a);
        void onError(Announcement a, String reason);
    }

    /** Whether this provider can handle the given announcement at all. */
    boolean supports(Announcement a);

    void play(Announcement a, Listener listener);

    /** Stop immediately, e.g. because an EMERGENCY item pre-empted this one. */
    void stop();

    void release();
}
