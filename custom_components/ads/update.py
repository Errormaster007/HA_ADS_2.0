"""Support for ADS update entities."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import ClientError

from homeassistant.components.update import UpdateEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).with_name("manifest.json")
_REPOSITORY = "Errormaster007/ads-custom-component"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{_REPOSITORY}/releases/latest"


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
    async_add_entities([AdsUpdateEntity(hass, entry.entry_id)])


class AdsUpdateEntity(UpdateEntity):
    """Represent whether a newer ADS release is available."""

    _attr_has_entity_name = True
    _attr_name = "Update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the update entity."""
        self.hass = hass
        self._entry_id = entry_id
        self._latest_version: str | None = None
        self._release_notes: str | None = None
        self._release_summary: str | None = None
        self._release_url: str | None = None
        self._attr_installed_version = _installed_version()
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_update"

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
            _LOGGER.debug("Could not fetch ADS release info: %s", err)
            self._latest_version = self._attr_installed_version
            self._release_notes = None
            self._release_summary = None
            self._release_url = None
            return

        latest_version = str(payload.get("tag_name", "")).lstrip("v")
        if not latest_version:
            self._latest_version = self._attr_installed_version
            self._release_notes = None
            self._release_summary = None
            self._release_url = None
            return

        self._latest_version = latest_version
        body = str(payload.get("body", "")).strip()
        self._release_notes = body or None
        self._release_summary = body[:255] if body else None
        self._release_url = str(payload.get("html_url", "")) or None
