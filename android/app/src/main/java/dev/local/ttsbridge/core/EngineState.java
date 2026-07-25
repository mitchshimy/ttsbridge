package dev.local.ttsbridge.core;

public enum EngineState {
    IDLE,
    SPEAKING,
    BUSY // reserved: room to report e.g. "provider unavailable, falling back"
}
