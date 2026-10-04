"""Support for mapped ADS number entities."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .mapped_entity import async_add_mapped_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up mapped ADS numbers."""
    async_add_mapped_entities(hass, entry, "number", async_add_entities)
