"""Support for Automation Device Specification (ADS)."""

from collections import namedtuple
from collections.abc import Callable
from contextlib import suppress
import ctypes
from datetime import datetime, timedelta
import logging
import struct
import threading

import pyads

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

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
        self._notification_configs = {}
        self._connection_callbacks = set()
        self._cancel_connection_monitor: Callable[[], None] | None = None
        self._connected = True
        self._missing_variables: list[dict[str, str]] = []
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        """Return whether the ADS connection is healthy."""
        return self._connected

    def register_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Register a callback for connection state changes."""
        self._connection_callbacks.add(callback)

    def unregister_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Unregister a connection state callback."""
        self._connection_callbacks.discard(callback)

    def start_connection_monitor(self, hass: HomeAssistant) -> Callable[[], None]:
        """Periodically verify the ADS connection and restore it if needed."""

        if self._cancel_connection_monitor is not None:
            self.stop_connection_monitor()

        async def _check_connection(_now: datetime) -> None:
            await hass.async_add_executor_job(self.check_connection)

        cancel = async_track_time_interval(
            hass, _check_connection, timedelta(seconds=30)
        )
        self._cancel_connection_monitor = cancel

        def _cancel_monitor() -> None:
            if self._cancel_connection_monitor is cancel:
                self._cancel_connection_monitor = None
                cancel()

        return _cancel_monitor

    def stop_connection_monitor(self) -> None:
        """Stop periodic ADS health checks."""
        if self._cancel_connection_monitor is not None:
            cancel, self._cancel_connection_monitor = (
                self._cancel_connection_monitor,
                None,
            )
            cancel()

    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        for callback in tuple(self._connection_callbacks):
            callback(connected)

    def check_connection(self) -> None:
        """Check ADS state, reconnect if needed, and restore subscriptions."""
        with self._lock:
            try:
                self._client.read_state()
            except pyads.ADSError as err:
                if self._connected:
                    _LOGGER.warning(
                        "ADS connection lost for hub '%s': %s", self._hub_id, err
                    )
                self._set_connected(False)
                self._reconnect()
                return

            if self._register_missing_notifications():
                self._set_connected(True)

    def _reconnect(self) -> None:
        with suppress(pyads.ADSError):
            self._client.close()

        try:
            self._client.open()
            self._client.read_state()
        except pyads.ADSError as err:
            with suppress(pyads.ADSError):
                self._client.close()
            _LOGGER.warning("Could not reconnect ADS hub '%s': %s", self._hub_id, err)
            return

        self._notification_items.clear()
        if self._register_missing_notifications():
            self._set_connected(True)
            _LOGGER.info("ADS connection restored for hub '%s'", self._hub_id)

    def _register_missing_notifications(self) -> bool:
        active = {
            (item.name, item.callback) for item in self._notification_items.values()
        }
        successful = True
        for key, item in tuple(self._notification_configs.items()):
            if key not in active:
                if (
                    not self._register_notification(item)
                    and key in self._notification_configs
                ):
                    successful = False
        return successful

    def _register_notification(self, item) -> bool:
        attr = pyads.NotificationAttrib(ctypes.sizeof(item.plc_datatype))
        try:
            hnotify, huser = self._client.add_device_notification(
                item.name, attr, self._device_notification_callback
            )
        except pyads.ADSError as err:
            if _is_missing_symbol_error(err):
                self._notification_configs.pop((item.name, item.callback), None)
                self.record_missing_variable(item.name, item.name, "notification")
                _LOGGER.debug(
                    "Hub '%s': Skipping missing variable '%s' (%s)",
                    self._hub_id,
                    item.name,
                    err,
                )
            else:
                self._set_connected(False)
                _LOGGER.error(
                    "Hub '%s': Error subscribing to %s: %s",
                    self._hub_id,
                    item.name,
                    err,
                )
            return False

        hnotify = int(hnotify)
        registered_item = item._replace(hnotify=hnotify, huser=huser)
        self._notification_items[hnotify] = registered_item
        try:
            value = self._client.read_by_name(item.name, item.plc_datatype)
        except pyads.ADSError as err:
            with suppress(pyads.ADSError):
                self._client.del_device_notification(hnotify, huser)
            self._notification_items.pop(hnotify, None)
            if _is_missing_symbol_error(err):
                self._notification_configs.pop((item.name, item.callback), None)
                self.record_missing_variable(item.name, item.name, "notification")
            else:
                self._set_connected(False)
            _LOGGER.debug(
                "Hub '%s': Could not read initial value for %s after subscribing: %s",
                self._hub_id,
                item.name,
                err,
            )
            return False
        else:
            item.callback(item.name, value)
        return True

    def shutdown(self, *args, **kwargs):
        """Shutdown ADS connection."""

        self.stop_connection_monitor()
        _LOGGER.debug("Shutting down ADS hub '%s'", self._hub_id)
        with self._lock:
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
                    _LOGGER.error(
                        "Error removing notification on hub '%s': %s",
                        self._hub_id,
                        err,
                    )
            try:
                self._client.close()
                _LOGGER.info("ADS connection closed for hub '%s'", self._hub_id)
            except pyads.ADSError as err:
                _LOGGER.error(
                    "Error closing ADS connection on hub '%s': %s", self._hub_id, err
                )

    def register_device(self, device):
        """Register a new device."""
        self._devices.append(device)

    @property
    def missing_variables(self) -> list[dict[str, str]]:
        """Return all missing ADS variables collected for debug output."""
        return list(self._missing_variables)

    def record_missing_variable(self, name: str, entity_name: str, source: str) -> None:
        """Store a missing ADS symbol once for later diagnostics."""
        item = {"name": name, "entity": entity_name, "source": source}
        if item not in self._missing_variables:
            self._missing_variables.append(item)

    def has_variable(self, name: str, plc_datatype) -> bool:
        """Check whether a PLC symbol exists without logging a hard error."""
        with self._lock:
            try:
                self._client.read_by_name(name, plc_datatype)
            except pyads.ADSError:
                return False

        return True

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
                if not _is_missing_symbol_error(err):
                    self._set_connected(False)
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
                if not _is_missing_symbol_error(err):
                    self._set_connected(False)
                _LOGGER.error("Hub '%s': Error reading %s: %s", self._hub_id, name, err)

    def read_mapped_variable(self, name: str, plc_datatype: type):
        """Read a mapped symbol without a success-shaped error fallback."""
        with self._lock:
            try:
                return self._client.read_by_name(name, plc_datatype)
            except pyads.ADSError as err:
                if not _is_missing_symbol_error(err):
                    self._set_connected(False)
                raise

    def write_mapped_variable(self, name: str, value, plc_datatype: type) -> None:
        """Write an explicitly mapped command and report failures to HA."""
        with self._lock:
            try:
                self._client.write_by_name(name, value, plc_datatype)
            except pyads.ADSError as err:
                if not _is_missing_symbol_error(err):
                    self._set_connected(False)
                raise HomeAssistantError(f"ADS write failed for {name}: {err}") from err

    def remove_mapped_notifications(self, callback: Callable) -> None:
        """Unsubscribe a mapped entity without disturbing other entities."""
        with self._lock:
            items = [
                item
                for item in self._notification_items.values()
                if item.callback == callback
            ]
            for item in items:
                self._client.del_device_notification(item.hnotify, item.huser)
                del self._notification_items[item.hnotify]
            self._notification_configs = {
                key: item
                for key, item in self._notification_configs.items()
                if item.callback != callback
            }

    def add_device_notification(self, name, plc_datatype, callback):
        """Add a notification to the ADS devices."""

        with self._lock:
            item = NotificationItem(0, 0, name, plc_datatype, callback)
            self._notification_configs[(name, callback)] = item
            return self._register_notification(item)

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
                "Hub '%s': Unknown device notification handle: %d",
                self._hub_id,
                hnotify,
            )
            return

        # Data parsing based on PLC data type
        plc_datatype = notification_item.plc_datatype
        unpack_formats = {
            pyads.PLCTYPE_BYTE: "<B",
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
            _LOGGER.warning(
                "Hub '%s': No callback available for this datatype", self._hub_id
            )

        _LOGGER.debug(
            "Hub '%s': Notification value update for '%s' -> '%s'",
            self._hub_id,
            notification_item.name,
            value,
        )

        notification_item.callback(notification_item.name, value)


def _is_missing_symbol_error(err: pyads.ADSError) -> bool:
    """Return True if an ADS error means that a symbol does not exist."""
    message = str(err).lower()
    return "symbol not found" in message or "1808" in message
