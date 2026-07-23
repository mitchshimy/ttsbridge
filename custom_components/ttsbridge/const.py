"""Constants for the TTS Bridge integration."""

DOMAIN = "ttsbridge"

DEFAULT_PORT = 8098

# Key under which the user's chosen androidtv media_player entity_id is
# stored in entry.data, if they opted in to automation installation
# during setup (config_flow.py's async_step_setup_automations). Absent
# entirely if they skipped that step.
CONF_MEDIA_PLAYER_ENTITY_ID = "media_player_entity_id"

# Key under which the generated (random, unguessable) webhook_id is
# persisted in the config entry's data, so it stays stable across HA
# restarts instead of being regenerated - and re-pushed to the device -
# every time.
CONF_WEBHOOK_ID = "webhook_id"
DEFAULT_PRIORITY = "normal"
DEFAULT_CATEGORY = "general"

# Same request-timeout budget used on the HA-side rest_command version of
# this integration - kept for continuity, not a hard technical requirement.
REQUEST_TIMEOUT_SECONDS = 10
