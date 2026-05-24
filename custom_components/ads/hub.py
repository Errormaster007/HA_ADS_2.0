"""Support for Automation Device Specification (ADS)."""

from collections import namedtuple
import ctypes
import logging
import struct
import threading

import pyads

_LOGGER = logging.getLogger(__name__)

# Tuple to hold data needed for notification
NotificationItem = namedtuple(  # noqa: PYI024
    "NotificationItem", "hnotify huser name plc_datatype callback"
)


class AdsHub:
    """Representation of an ADS connection."""

    def __init__(self, ads_client, hub_id: str = "default"):
        """Initialize the ADS hub."""
        self._hub_id = hub_id
        self._client = ads_client
        self._client.open()
        _LOGGER.info("ADS connection opened for hub '%s'", self._hub_id)

        # All ADS devices are registered here
        self._devices = []
        self._notification_items = {}
        self._lock = threading.Lock()

    def shutdown(self, *args, **kwargs):
        """Shutdown ADS connection."""

        _LOGGER.debug("Shutting down ADS hub '%s'", self._hub_id)
        for notification_item in self._notification_items.values():
            _LOGGER.debug(
                "Deleting device notification %d, %d (hub '%s')",
                notification_item.hnotify,
                notification_item.huser,
                self._hub_id,
            )
            try:
                self._client.del_device_notification(
                    notification_item.hnotify, notification_item.huser
                )
            except pyads.ADSError as err:
                _LOGGER.error("Error removing notification on hub '%s': %s", self._hub_id, err)
        try:
            self._client.close()
            _LOGGER.info("ADS connection closed for hub '%s'", self._hub_id)
        except pyads.ADSError as err:
            _LOGGER.error("Error closing ADS connection on hub '%s': %s", self._hub_id, err)

    def register_device(self, device):
        """Register a new device."""
        self._devices.append(device)

    def write_by_name(self, name, value, plc_datatype):
        """Write a value to the device."""

        _LOGGER.debug(
            "Hub '%s': writing variable '%s' with value '%s' and datatype '%s'",
            self._hub_id,
            name,
            value,
            plc_datatype,
        )
        with self._lock:
            try:
                return self._client.write_by_name(name, value, plc_datatype)
            except pyads.ADSError as err:
                _LOGGER.error("Hub '%s': Error writing %s: %s", self._hub_id, name, err)

    def read_by_name(self, name, plc_datatype):
        """Read a value from the device."""

        _LOGGER.debug(
            "Hub '%s': reading variable '%s' with datatype '%s'",
            self._hub_id,
            name,
            plc_datatype,
        )
        with self._lock:
            try:
                return self._client.read_by_name(name, plc_datatype)
            except pyads.ADSError as err:
                _LOGGER.error("Hub '%s': Error reading %s: %s", self._hub_id, name, err)

    def add_device_notification(self, name, plc_datatype, callback):
        """Add a notification to the ADS devices."""

        attr = pyads.NotificationAttrib(ctypes.sizeof(plc_datatype))

        with self._lock:
            try:
                hnotify, huser = self._client.add_device_notification(
                    name, attr, self._device_notification_callback
                )
            except pyads.ADSError as err:
                _LOGGER.error("Hub '%s': Error subscribing to %s: %s", self._hub_id, name, err)
            else:
                hnotify = int(hnotify)
                self._notification_items[hnotify] = NotificationItem(
                    hnotify, huser, name, plc_datatype, callback
                )

                _LOGGER.debug(
                    "Hub '%s': Added device notification %d for variable %s",
                    self._hub_id,
                    hnotify,
                    name,
                )

    def _device_notification_callback(self, notification, name):
        """Handle device notifications."""
        contents = notification.contents
        hnotify = int(contents.hNotification)
        _LOGGER.debug("Hub '%s': Received notification %d", self._hub_id, hnotify)

        # Get dynamically sized data array
        data_size = contents.cbSampleSize
        data_address = (
            ctypes.addressof(contents)
            + pyads.structs.SAdsNotificationHeader.data.offset
        )
        data = (ctypes.c_ubyte * data_size).from_address(data_address)

        # Acquire notification item
        with self._lock:
            notification_item = self._notification_items.get(hnotify)

        if not notification_item:
            _LOGGER.error(
                "Hub '%s': Unknown device notification handle: %d", self._hub_id, hnotify
            )
            return

        # Data parsing based on PLC data type
        plc_datatype = notification_item.plc_datatype
        unpack_formats = {
            pyads.PLCTYPE_BYTE: "<b",
            pyads.PLCTYPE_INT: "<h",
            pyads.PLCTYPE_UINT: "<H",
            pyads.PLCTYPE_SINT: "<b",
            pyads.PLCTYPE_USINT: "<B",
            pyads.PLCTYPE_DINT: "<i",
            pyads.PLCTYPE_UDINT: "<I",
            pyads.PLCTYPE_WORD: "<H",
            pyads.PLCTYPE_DWORD: "<I",
            pyads.PLCTYPE_LREAL: "<d",
            pyads.PLCTYPE_REAL: "<f",
            pyads.PLCTYPE_TOD: "<i",  # Treat as DINT
            pyads.PLCTYPE_DATE: "<i",  # Treat as DINT
            pyads.PLCTYPE_DT: "<i",  # Treat as DINT
            pyads.PLCTYPE_TIME: "<i",  # Treat as DINT
        }

        if plc_datatype == pyads.PLCTYPE_BOOL:
            value = bool(struct.unpack("<?", bytearray(data))[0])
        elif plc_datatype == pyads.PLCTYPE_STRING:
            value = (
                bytearray(data).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            )
        elif plc_datatype in unpack_formats:
            value = struct.unpack(unpack_formats[plc_datatype], bytearray(data))[0]
        else:
            value = bytearray(data)
            _LOGGER.warning("Hub '%s': No callback available for this datatype", self._hub_id)

        _LOGGER.debug(
            "Hub '%s': Notification value update for '%s' -> '%s'",
            self._hub_id,
            notification_item.name,
            value,
        )

        notification_item.callback(notification_item.name, value)
