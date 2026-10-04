"""Support for ADS update entities."""

import asyncio
from datetime import timedelta
import json
import logging
from pathlib import Path

from aiohttp import ClientError

from homeassistant.components import persistent_notification
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=6)

_MANIFEST_PATH = Path(__file__).with_name("manifest.json")
_REPOSITORY = "Errormaster007/HA_ADS_2.0"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{_REPOSITORY}/releases/latest"
_NOTIFICATION_ID = f"{DOMAIN}_update_available"
_NOTIFICATION_LOCK_KEY = f"{DOMAIN}_update_notification_lock"


def _installed_version() -> str:
    """Read the installed integration version from manifest.json."""
    try:
        with _MANIFEST_PATH.open(encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except OSError as err:
        _LOGGER.warning("Could not read ADS manifest version: %s", err)
        return "0.0.0"

    return str(manifest.get("version", "0.0.0"))


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ADS update entity."""
    installed_version = await hass.async_add_executor_job(_installed_version)
    async_add_entities(
        [AdsUpdateEntity(hass, entry.entry_id, installed_version)],
        update_before_add=True,
    )


class AdsUpdateEntity(UpdateEntity):
    """Represent whether a newer ADS release is available."""

    _attr_has_entity_name = True
    _attr_name = "Update"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES

    def __init__(
        self, hass: HomeAssistant, entry_id: str, installed_version: str
    ) -> None:
        """Initialize the update entity."""
        self.hass = hass
        self._entry_id = entry_id
        self._latest_version: str | None = None
        self._release_notes: str | None = None
        self._release_summary: str | None = None
        self._release_url: str | None = None
        self._attr_installed_version = installed_version
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_update"
        self._notification_store: Store[dict[str, list[str]]] | None = None
        self._notified_versions: set[str] | None = None

    @property
    def title(self) -> str:
        """Return the title shown for the update entity."""
        return "ADS"

    @property
    def latest_version(self) -> str | None:
        """Return the latest available version."""
        return self._latest_version

    @property
    def release_summary(self) -> str | None:
        """Return a short release summary."""
        return self._release_summary

    @property
    def release_url(self) -> str | None:
        """Return the release notes URL."""
        return self._release_url

    async def async_release_notes(self) -> str | None:
        """Return the full release notes for the latest version."""
        return self._release_notes

    async def async_update(self) -> None:
        """Fetch the latest release information from GitHub."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                _LATEST_RELEASE_URL,
                headers={"Accept": "application/vnd.github+json"},
                timeout=15,
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Could not fetch ADS release info: %s", err)
            return

        latest_version = str(payload.get("tag_name", "")).lstrip("v")
        if not latest_version:
            _LOGGER.warning("GitHub returned no latest ADS release tag")
            return

        self._latest_version = latest_version
        body = str(payload.get("body", "")).strip()
        self._release_notes = body or None
        self._release_summary = body[:255] if body else None
        self._release_url = str(payload.get("html_url", "")) or None
        if self.state == STATE_ON:
            await self._async_notify_update_available(latest_version)
        else:
            persistent_notification.async_dismiss(self.hass, _NOTIFICATION_ID)

    async def _async_notify_update_available(self, latest_version: str) -> None:
        """Create one persistent notification for each newly available version."""
        lock: asyncio.Lock = self.hass.data.setdefault(
            _NOTIFICATION_LOCK_KEY, asyncio.Lock()
        )
        if self._notification_store is None:
            self._notification_store = Store(
                self.hass, 1, f"{DOMAIN}_update_notifications"
            )
        async with lock:
            if self._notified_versions is None:
                saved = await self._notification_store.async_load()
                self._notified_versions = (
                    set(saved.get("versions", [])) if saved else set()
                )
            if latest_version in self._notified_versions:
                return

            release_url = self._release_url or (
                f"https://github.com/{_REPOSITORY}/releases"
            )
            persistent_notification.async_create(
                self.hass,
                (
                    f"ADS-Version **{latest_version}** ist verfügbar "
                    f"(installiert: {self.installed_version}). Aktualisiere ADS über "
                    f"HACS oder öffne die [Release-Seite]({release_url})."
                ),
                title="ADS-Update verfügbar",
                notification_id=_NOTIFICATION_ID,
            )
            self._notified_versions.add(latest_version)
            await self._notification_store.async_save(
                {"versions": sorted(self._notified_versions)}
            )
