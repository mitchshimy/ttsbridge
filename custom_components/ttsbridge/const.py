"""Constants for the TTS Bridge integration."""

DOMAIN = "ttsbridge"

DEFAULT_PORT = 8098

# Key under which the user's chosen androidtv media_player entity_id is
# stored in entry.data, if they opted in to automation installation
# during setup (config_flow.py's async_step_setup_automations). Absent
# entirely if they skipped that step.
CONF_MEDIA_PLAYER_ENTITY_ID = "media_player_entity_id"

# Separate, optional entity used only for the power-on automation's state
# trigger - decoupled from CONF_MEDIA_PLAYER_ENTITY_ID because the entity
# that can run adb_command (must be from the classic ADB-based androidtv
# integration) is often NOT the most reliable one to watch for state
# changes on. ADB-polled entities flap through "unavailable" on every
# WiFi/ADB session hiccup, which made the power-on automation misfire
# constantly when it was forced to share the same entity. A stabler
# entity (e.g. from androidtv_remote, which is push-based rather than
# ADB-polled) can be used here instead, while adb_command still targets
# the ADB-capable one. Falls back to CONF_MEDIA_PLAYER_ENTITY_ID if unset.
CONF_TRIGGER_ENTITY_ID = "trigger_entity_id"

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
