package dev.local.ttsbridge.core;

/**
 * Lower ordinal = higher priority. EMERGENCY always interrupts whatever is
 * currently playing, regardless of that announcement's "interruptible" flag.
 */
public enum Priority {
    EMERGENCY(0),
    HIGH(1),
    NORMAL(2),
    LOW(3);

    public final int rank;

    Priority(int rank) {
        this.rank = rank;
    }

    public static Priority fromString(String s, Priority fallback) {
        if (s == null) return fallback;
        try {
            return Priority.valueOf(s.trim().toUpperCase());
        } catch (IllegalArgumentException e) {
            return fallback;
        }
    }
}
