"""Support for Automation Device Specification (ADS)."""

from enum import StrEnum
from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .hub import AdsHub

DOMAIN = "ads"

DATA_ADS: HassKey[AdsHub] = HassKey(DOMAIN)
DATA_ADS_HUBS: HassKey[dict[str, AdsHub]] = HassKey(f"{DOMAIN}_hubs")

CONF_ADS_VAR = "adsvar"
CONF_GVL = "gvl"
CONF_GVL_FILE_PATH = "gvl_file_path"
CONF_GVL_IMPORT_REPLACE = "gvl_import_replace"
CONF_GVL_VARIABLES = "gvl_variables"
CONF_LEGACY_ENTITIES = "legacy_entities"
CONF_VERBOSE_LOGGING = "verbose_logging"
CONF_ENTRY_ID = "entry_id"

STATE_KEY_STATE = "state"


class AdsType(StrEnum):
    """Supported Types."""

    BOOL = "bool"
    BYTE = "byte"
    INT = "int"
    UINT = "uint"
    SINT = "sint"
    USINT = "usint"
    DINT = "dint"
    UDINT = "udint"
    WORD = "word"
    DWORD = "dword"
    LREAL = "lreal"
    REAL = "real"
    STRING = "string"
    TIME = "time"
    DATE = "date"
    DATE_AND_TIME = "dt"
    TOD = "tod"
