"""Constants for the TTS Bridge integration."""

DOMAIN = "ttsbridge"

DEFAULT_PORT = 8098
DEFAULT_PRIORITY = "normal"
DEFAULT_CATEGORY = "general"

# Same request-timeout budget used on the HA-side rest_command version of
# this integration - kept for continuity, not a hard technical requirement.
REQUEST_TIMEOUT_SECONDS = 10
