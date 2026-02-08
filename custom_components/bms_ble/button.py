"""Support for BMS_BLE buttons."""

from typing import Final

from aiobmsble.bms.jbd_bms import BMS as JbdBms

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BTBmsConfigEntry
from .const import DOMAIN
from .coordinator import BTBmsCoordinator

PARALLEL_UPDATES = 0

BUTTON_TYPES: Final[list[ButtonEntityDescription]] = [
    ButtonEntityDescription(
        key="reset_software_lock",
        translation_key="reset_software_lock",
        icon="mdi:lock-reset",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    )
]


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: BTBmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add buttons for passed config_entry in Home Assistant."""
    bms: BTBmsCoordinator = config_entry.runtime_data

    if not isinstance(bms.device, JbdBms):
        return

    unique_id = format_mac(config_entry.unique_id)
    async_add_entities([BMSResetSoftwareLockButton(bms, BUTTON_TYPES[0], unique_id)])


class BMSResetSoftwareLockButton(
    CoordinatorEntity[BTBmsCoordinator], ButtonEntity
):
    """Button to reset JBD software lock (MOS control)."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        bms: BTBmsCoordinator,
        descr: ButtonEntityDescription,
        unique_id: str,
    ) -> None:
        """Initialize the reset software lock button."""
        self._attr_unique_id = f"{DOMAIN}-{unique_id}-{descr.key}"
        self._attr_device_info = bms.device_info
        self._attr_has_entity_name = True
        self.entity_description = descr
        super().__init__(bms)

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.async_reset_software_lock()
        except TimeoutError as err:
            raise HomeAssistantError(
                "Timed out while sending MOS reset to JBD BMS."
            ) from err
