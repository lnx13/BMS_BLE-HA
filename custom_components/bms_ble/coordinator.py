"""Home Assistant coordinator for BLE Battery Management System integration."""

import asyncio
from collections import deque
from datetime import timedelta
from time import monotonic
from typing import Final

from aiobmsble import BMSInfo, BMSSample
from aiobmsble.basebms import BaseBMS
from aiobmsble.bms.jbd_bms import BMS as JbdBms
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from habluetooth import BluetoothServiceInfoBleak

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER, UPDATE_INTERVAL


class BTBmsCoordinator(DataUpdateCoordinator[BMSSample]):
    """Update coordinator for a battery management system."""

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        bms_device: BaseBMS,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize BMS data coordinator."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=config_entry.title,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            always_update=False,  # only update when sensor value has changed
            config_entry=config_entry,
        )
        self._device: Final[BaseBMS] = bms_device
        self._command_lock: Final[asyncio.Lock] = asyncio.Lock()
        self._link_q: deque[bool] = deque(
            [False], maxlen=100
        )  # track BMS update issues
        self._mac: Final[str] = ble_device.address
        self._stale: bool = False  # indicates no BMS response for significant time

        LOGGER.debug(
            "Initializing coordinator for %s (%s) as %s",
            self.name,
            self._mac,
            bms_device.bms_id(),
        )

        if service_info := async_last_service_info(
            self.hass, address=self._mac, connectable=True
        ):
            LOGGER.debug("%s: advertisement: %s", self.name, service_info.as_dict())

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac), (BLUETOOTH_DOMAIN, self._mac)},
            connections={(CONNECTION_BLUETOOTH, self._mac)},
        )

    @property
    def rssi(self) -> int | None:
        """Return RSSI value for target BMS."""

        service_info: BluetoothServiceInfoBleak | None = async_last_service_info(
            self.hass, address=self._mac, connectable=True
        )
        return service_info.rssi if service_info else None

    def _rssi_msg(self) -> str:
        """Return check RSSI message if below -75dBm."""
        return (
            f", check signal strength ({self.rssi} dBm)"
            if self.rssi and self.rssi < -75
            else ""
        )

    @property
    def link_quality(self) -> int:
        """Gives the percentage of successful BMS reads out of the last 100 attempts."""

        return self._link_q.count(True) * 100 // len(self._link_q)

    @property
    def device(self) -> BaseBMS:
        """Return the underlying BMS device."""
        return self._device

    async def async_shutdown(self) -> None:
        """Shutdown coordinator and any connection."""
        LOGGER.debug("Shutting down BMS (%s)", self.name)
        await super().async_shutdown()
        await self._device.disconnect()

    def _device_stale(self) -> bool:
        if self._link_q[-1]:
            self._stale = False
        elif (
            not self._stale
            and self.link_quality <= 10
            and list(self._link_q)[-10:] == [False] * 10
        ):
            LOGGER.error(
                "%s: BMS is stale, triggering reconnect%s!",
                self.name,
                self._rssi_msg(),
            )
            self._stale = True

        return self._stale

    async def _async_setup(self) -> None:
        bms_info: Final[BMSInfo] = await self._device.device_info()
        self.device_info.update(
            DeviceInfo(
                name=bms_info.get("name") or self.name,
                manufacturer=bms_info.get("manufacturer")
                or self._device.INFO.get("default_manufacturer"),
                model=bms_info.get("model") or self._device.INFO.get("default_model"),
                sw_version=bms_info.get("sw_version") or bms_info.get("fw_version"),
                hw_version=bms_info.get("hw_version"),
                model_id=bms_info.get("model_id"),
                serial_number=bms_info.get("serial_number"),
            )
        )

    async def _async_update_data(self) -> BMSSample:
        """Return the latest data from the device."""

        LOGGER.debug("%s: BMS data update", self.name)

        async with self._command_lock:
            if self._device_stale():
                await self._device.disconnect(reset=True)

            start: Final[float] = monotonic()
            try:
                if not (bms_data := await self._device.async_update()):
                    LOGGER.debug("%s: no valid data received", self.name)
                    raise UpdateFailed("no valid data received.")
            except TimeoutError as err:
                LOGGER.debug(
                    "%s: BMS communication timed out%s", self.name, self._rssi_msg()
                )
                raise TimeoutError("BMS communication timed out") from err
            except (BleakError, EOFError) as err:
                LOGGER.debug(
                    "%s: BMS communication failed%s: %s (%s)",
                    self.name,
                    self._rssi_msg(),
                    err,
                    type(err).__name__,
                )
                raise UpdateFailed(
                    f"BMS communication failed{self._rssi_msg()}: {err!s} ({type(err).__name__})"
                ) from err
            finally:
                self._link_q.extend(
                    [False] * (1 + int((monotonic() - start) / UPDATE_INTERVAL))
                )

        self._link_q[-1] = True  # set success
        LOGGER.debug("%s: BMS data sample %s", self.name, bms_data)

        return bms_data

    async def async_reset_software_lock(self) -> None:
        """Reset JBD software lock (MOS control) by enabling charge/discharge."""
        if not isinstance(self._device, JbdBms):
            raise HomeAssistantError("Reset software lock is only supported for JBD BMS.")

        software_lock_bit: Final[int] = 1 << 12

        # JBD write command: 0xDD 0x5A <cmd> <len> <data...> <crc_hi> <crc_lo> 0x77
        cmd: Final[int] = 0xE1  # MOS control register
        data: Final[bytes] = bytes([0x00, 0x00])  # enable charge & discharge
        frame = bytearray([0xDD, 0x5A, cmd, len(data), *data])
        crc = (0x10000 - sum(frame[2:])) & 0xFFFF
        frame.extend(crc.to_bytes(2, "big"))
        frame.append(0x77)

        async with self._command_lock:
            await self._device._connect()
            # Clear any stale frame to avoid mixing responses.
            if hasattr(self._device, "_frame"):
                self._device._frame.clear()
            if hasattr(self._device, "_msg"):
                self._device._msg = b""
            if hasattr(self._device, "_valid_reply"):
                self._device._valid_reply = cmd

            try:
                await self._device._await_msg(bytes(frame), wait_for_notify=True)
            except TimeoutError:
                LOGGER.debug(
                    "%s: no response to MOS reset, sending without response", self.name
                )
                await self._device._await_msg(bytes(frame), wait_for_notify=False)
            finally:
                if hasattr(self._device, "_valid_reply"):
                    self._device._valid_reply = 0x00

        # Auto-check: refresh state a few times to verify lock cleared.
        for attempt in range(3):
            await asyncio.sleep(0.5 if attempt else 1.0)
            await self.async_refresh()

            data = self.data
            if not data:
                continue

            problem_code = data.get("problem_code")
            if isinstance(problem_code, int):
                if problem_code & software_lock_bit:
                    continue
                return

            chrg = data.get("chrg_mosfet")
            dis = data.get("dischrg_mosfet")
            if chrg is None and dis is None:
                continue
            if bool(chrg) and bool(dis):
                return

        raise HomeAssistantError(
            "Software lock still active or status unavailable after reset command."
        )
