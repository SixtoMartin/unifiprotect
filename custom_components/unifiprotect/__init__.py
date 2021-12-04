"""Unifi Protect Platform."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import functools
import logging

from aiohttp import CookieJar
from aiohttp.client_exceptions import ServerDisconnectedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_ID,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from pyunifiprotect import NotAuthorized, NvrError, ProtectApiClient
from pyunifiprotect.data.nvr import NVR
from pyunifiprotect.data.types import ModelType

from .const import (
    CONF_ALL_UPDATES,
    CONF_DISABLE_RTSP,
    CONF_DOORBELL_TEXT,
    CONFIG_OPTIONS,
    DEFAULT_BRAND,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_TYPE_CAMERA,
    DEVICES_FOR_SUBSCRIBE,
    DEVICES_WITH_ENTITIES,
    DOMAIN,
    DOORBELL_TEXT_SCHEMA,
    MIN_REQUIRED_PROTECT_V,
    PLATFORMS,
    PLATFORMS_NEXT,
    PROFILE_WS_SCHEMA,
    SERVICE_ADD_DOORBELL_TEXT,
    SERVICE_PROFILE_WS,
    SERVICE_REMOVE_DOORBELL_TEXT,
    SERVICE_SET_DEFAULT_DOORBELL_TEXT,
)
from .data import UnifiProtectData
from .models import UnifiProtectEntryData
from .services import (
    add_doorbell_text,
    profile_ws,
    remove_doorbell_text,
    set_default_doorbell_text,
)
from .utils import above_ha_version

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)


@callback
async def _async_migrate_data(
    hass: HomeAssistant, entry: ConfigEntry, protect: ProtectApiClient
) -> None:
    # already up to date, skip
    if CONF_ALL_UPDATES in entry.options:
        return

    _LOGGER.info("Starting entity migration...")

    # migrate entry
    options = dict(entry.options)
    options[CONF_ALL_UPDATES] = False
    if CONF_DOORBELL_TEXT in options:
        del options[CONF_DOORBELL_TEXT]
    hass.config_entries.async_update_entry(entry, data=entry.data, options=options)

    # migrate entities
    registry = er.async_get(hass)
    mac_to_id: dict[str, str] = {}
    mac_to_channel_id: dict[str, str] = {}
    bootstrap = await protect.get_bootstrap()
    for model in DEVICES_WITH_ENTITIES:
        attr = model.value + "s"
        for device in getattr(bootstrap, attr).values():
            mac_to_id[device.mac] = device.id
            if model != ModelType.CAMERA:
                continue

            for channel in device.channels:
                channel_id = str(channel.id)
                if channel.is_rtsp_enabled:
                    break
            mac_to_channel_id[device.mac] = channel_id

    count = 0
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    for entity in entities:
        new_unique_id: str | None = None
        if entity.domain != DEVICE_TYPE_CAMERA:
            parts = entity.unique_id.split("_")
            if len(parts) >= 2:
                device_or_key = "_".join(parts[:-1])
                mac = parts[-1]

                device_id = mac_to_id[mac]
                if device_or_key == device_id:
                    new_unique_id = device_id
                else:
                    new_unique_id = f"{device_id}_{device_or_key}"
        else:
            parts = entity.unique_id.split("_")
            if len(parts) == 2:
                mac = parts[1]
                device_id = mac_to_id[mac]
                channel_id = mac_to_channel_id[mac]
                new_unique_id = f"{device_id}_{channel_id}"
            else:
                device_id = parts[0]
                channel_id = parts[2]
                extra = "" if len(parts) == 3 else "_insecure"
                new_unique_id = f"{device_id}_{channel_id}{extra}"

        if new_unique_id is None:
            continue

        _LOGGER.debug(
            "Migrating entity %s (old unique_id: %s, new unique_id: %s)",
            entity.entity_id,
            entity.unique_id,
            new_unique_id,
        )
        try:
            registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
        except ValueError:
            _LOGGER.warning(
                "Could not migrate entity %s (old unique_id: %s, new unique_id: %s)",
                entity.entity_id,
                entity.unique_id,
                new_unique_id,
            )
        else:
            count += 1

    _LOGGER.info("Migrated %s entities", count)
    if count != len(entities):
        _LOGGER.warning("%s entities not migrated", len(entities) - count)


@callback
def _async_import_options_from_data_if_missing(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    options = dict(entry.options)
    data = dict(entry.data)
    modified = False
    for importable_option in CONFIG_OPTIONS:
        if importable_option not in entry.options and importable_option in entry.data:
            options[importable_option] = entry.data[importable_option]
            del data[importable_option]
            modified = True

    if modified:
        hass.config_entries.async_update_entry(entry, data=data, options=options)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Unifi Protect config entries."""
    _async_import_options_from_data_if_missing(hass, entry)

    session = async_create_clientsession(hass, cookie_jar=CookieJar(unsafe=True))
    protect = ProtectApiClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        verify_ssl=entry.data[CONF_VERIFY_SSL],
        session=session,
        subscribed_models=DEVICES_FOR_SUBSCRIBE,
        ignore_stats=not entry.options.get(CONF_ALL_UPDATES, False),
    )
    _LOGGER.debug("Connect to UniFi Protect")
    protect_data = UnifiProtectData(hass, protect, SCAN_INTERVAL, entry)

    try:
        nvr_info = await protect.get_nvr()
    except NotAuthorized as err:
        raise ConfigEntryAuthFailed(err) from err
    except (asyncio.TimeoutError, NvrError, ServerDisconnectedError) as notreadyerror:
        raise ConfigEntryNotReady from notreadyerror

    if nvr_info.version < MIN_REQUIRED_PROTECT_V:
        _LOGGER.error(
            (
                "You are running v%s of UniFi Protect. Minimum required version is v%s. "
                "Please upgrade UniFi Protect and then retry"
            ),
            nvr_info.version,
            MIN_REQUIRED_PROTECT_V,
        )
        return False

    await _async_migrate_data(hass, entry, protect)
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=nvr_info.mac)

    await protect_data.async_setup()
    if not protect_data.last_update_success:
        raise ConfigEntryNotReady

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = UnifiProtectEntryData(
        protect_data=protect_data,
        protect=protect,
        disable_stream=entry.options.get(CONF_DISABLE_RTSP, False),
    )

    await _async_get_or_create_nvr_device_in_registry(hass, entry, nvr_info)

    if above_ha_version(2021, 12):
        hass.config_entries.async_setup_platforms(entry, PLATFORMS_NEXT)
    else:
        hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_DOORBELL_TEXT,
        functools.partial(add_doorbell_text, hass),
        DOORBELL_TEXT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_DOORBELL_TEXT,
        functools.partial(remove_doorbell_text, hass),
        DOORBELL_TEXT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_DEFAULT_DOORBELL_TEXT,
        functools.partial(set_default_doorbell_text, hass),
        DOORBELL_TEXT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PROFILE_WS,
        functools.partial(profile_ws, hass),
        PROFILE_WS_SCHEMA,
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, protect_data.async_stop)
    )

    return True


async def _async_get_or_create_nvr_device_in_registry(
    hass: HomeAssistant, entry: ConfigEntry, nvr: NVR
) -> None:
    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, nvr.mac)},
        identifiers={(DOMAIN, nvr.mac)},
        manufacturer=DEFAULT_BRAND,
        name=entry.data[CONF_ID],
        model=nvr.type,
        sw_version=str(nvr.version),
        configuration_url=nvr.api.base_url,
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Unifi Protect config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data: UnifiProtectEntryData = hass.data[DOMAIN][entry.entry_id]
        await data.protect_data.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id)
    return bool(unload_ok)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        new = {**config_entry.data}
        # keep verify SSL false for anyone migrating to maintain backwards compatibility
        new[CONF_VERIFY_SSL] = False
        if CONF_DOORBELL_TEXT in new:
            del new[CONF_DOORBELL_TEXT]

        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
