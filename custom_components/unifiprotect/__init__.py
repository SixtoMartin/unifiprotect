"""Unifi Protect Platform."""

import asyncio
from datetime import timedelta
import logging

from aiohttp import CookieJar
from aiohttp.client_exceptions import ServerDisconnectedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_ID,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import homeassistant.helpers.device_registry as dr
from pyunifiprotect import NotAuthorized, NvrError, UpvServer
from pyunifiprotect.const import SERVER_ID

from .const import (
    CONF_DOORBELL_TEXT,
    CONF_DISABLE_RTSP,
    CONF_SNAPSHOT_DIRECT,
    DEFAULT_BRAND,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_REQUIRED_PROTECT_V,
    UNIFI_PROTECT_PLATFORMS,
)
from .data import UnifiProtectData

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)


@callback
def _async_normalize_config_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move options from data for imported entries.
    Initialize options with default values for other entries.
    Copy the unique id to CONF_ID if it is missing
    """
    if not entry.options:
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_HOST: entry.data.get(CONF_HOST),
                CONF_ID: entry.data.get(CONF_ID) or entry.unique_id,
                CONF_PORT: entry.data.get(CONF_PORT),
                CONF_USERNAME: entry.data.get(CONF_USERNAME),
                CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
            },
            options={
                CONF_USERNAME: entry.data.get(CONF_USERNAME),
                CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
                CONF_SNAPSHOT_DIRECT: entry.data.get(CONF_SNAPSHOT_DIRECT, False),
                CONF_DISABLE_RTSP: entry.data.get(CONF_DISABLE_RTSP, False),
                CONF_DOORBELL_TEXT: entry.data.get(CONF_DOORBELL_TEXT, ""),
            },
            unique_id=entry.unique_id or entry.data.get(CONF_ID),
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Unifi Protect config entries."""
    _async_normalize_config_entry(hass, entry)

    session = async_create_clientsession(hass, cookie_jar=CookieJar(unsafe=True))
    protectserver = UpvServer(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = protectserver
    _LOGGER.debug("Connect to Unfi Protect")

    events_update_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    protect_data = UnifiProtectData(
        hass, protectserver, timedelta(seconds=events_update_interval)
    )

    try:
        nvr_info = await protectserver.server_information()
        if nvr_info["server_version"] < MIN_REQUIRED_PROTECT_V:
            _LOGGER.error(
                "You are running V%s of UniFi Protect. Minimum required version is V%s. Please upgrade UniFi Protect and then retry",
                nvr_info["server_version"],
                MIN_REQUIRED_PROTECT_V,
            )
            return False

    except NotAuthorized:
        _LOGGER.error(
            "Could not Authorize against Unifi Protect. Please reinstall the Integration"
        )
        return False
    except (asyncio.TimeoutError, NvrError, ServerDisconnectedError) as notreadyerror:
        raise ConfigEntryNotReady from notreadyerror

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=nvr_info[SERVER_ID])

    await protect_data.async_setup()
    if not protect_data.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = {
        "protect_data": protect_data,
        "upv": protectserver,
        "snapshot_direct": entry.options.get(CONF_SNAPSHOT_DIRECT, False),
        "server_info": nvr_info,
        "disable_stream": entry.options.get(CONF_DISABLE_RTSP, False),
        "doorbell_text": entry.options.get(CONF_DOORBELL_TEXT, None),
    }

    await _async_get_or_create_nvr_device_in_registry(hass, entry, nvr_info)

    hass.config_entries.async_setup_platforms(entry, UNIFI_PROTECT_PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, protect_data.async_stop)
    )

    return True


async def _async_get_or_create_nvr_device_in_registry(
    hass: HomeAssistant, entry: ConfigEntry, nvr
) -> None:
    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, nvr["server_id"])},
        identifiers={(DOMAIN, nvr["server_id"])},
        manufacturer=DEFAULT_BRAND,
        name=entry.data[CONF_ID],
        model=nvr["server_model"],
        sw_version=nvr["server_version"],
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Unifi Protect config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, UNIFI_PROTECT_PLATFORMS
    )

    if unload_ok:
        data = hass.data[DOMAIN][entry.entry_id]
        await data["protect_data"].async_stop()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
