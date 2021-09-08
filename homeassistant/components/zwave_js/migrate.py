"""Functions used to migrate unique IDs for Z-Wave JS entities."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import cast

from zwave_js_server.client import Client as ZwaveClient
from zwave_js_server.model.value import Value as ZwaveValue

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import (
    DeviceEntry,
    async_get as async_get_device_registry,
)
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    RegistryEntry,
    async_entries_for_config_entry,
    async_entries_for_device,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.singleton import singleton
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .discovery import ZwaveDiscoveryInfo
from .helpers import get_device_id, get_unique_id

_LOGGER = logging.getLogger(__name__)

LEGACY_ZWAVE_MIGRATION = f"{DOMAIN}_legacy_zwave_migration"
STORAGE_WRITE_DELAY = 30
STORAGE_KEY = f"{DOMAIN}.legacy_zwave_migration"
STORAGE_VERSION = 1

# Use the following data to map entity entries
# between zwave and zwave_js:
# entity domain
# node id
# command class
# node instance (index 1) to endpoint index (index 0)
# unit of measurement
# label map to property if map has item

# Create maps for all CCs and platforms
# where there is more than one entity per CC.

# CC 49: https://github.com/zwave-js/node-zwave-js/blob/master/packages/config/config/sensorTypes.json
# Map: label to propertyName
# CC 50: https://github.com/zwave-js/node-zwave-js/blob/master/packages/config/config/meters.json
# Map: label to propertyKeyName
# CC 113: https://github.com/zwave-js/node-zwave-js/blob/master/packages/config/config/notifications.json
# Map: label to propertyName
# TODO: How to map CC 113 completely.
# Something corresponding to propertyKeyName missing in zwave
# Check if we can get the meterType from openzwave

# Update zwave_js migration to be similar to zwave migration.
# Include the migration mapping logic in a function.
# Write a websocket command for zwave_js migration.

NOTIFICATION_CC_LABEL_TO_PROPERTY_NAME = {
    "Smoke": "Smoke Alarm",
    "Carbon Monoxide": "CO Alarm",
    "Carbon Dioxide": "CO2 Alarm",
    "Heat": "Heat Alarm",
    "Flood": "Water Alarm",
    "Access Control": "Access Control",
    "Burglar": "Home Security",
    "Power Management": "Power Management",
    "System": "System",
    "Emergency": "Siren",
    "Clock": "Clock",
    "Appliance": "Appliance",
    "HomeHealth": "Home Health",
}

SENSOR_MULTILEVEL_CC_LABEL_TO_PROPERTY_NAME = {
    "Temperature": "Air temperature",
    "General": "General purpose",
    "Luminance": "Illuminance",
    "Power": "Power",
    "Relative Humidity": "Humidity",
    "Velocity": "Velocity",
    "Direction": "Direction",
    "Atmospheric Pressure": "Atmospheric pressure",
    "Barometric Pressure": "Barometric pressure",
    "Solar Radiation": "Solar radiation",
    "Dew Point": "Dew point",
    "Rain Rate": "Rain rate",
    "Tide Level": "Tide level",
    "Weight": "Weight",
    "Voltage": "Voltage",
    "Current": "Current",
    "CO2 Level": "Carbon dioxide (CO₂) level",
    "Air Flow": "Air flow",
    "Tank Capacity": "Tank capacity",
    "Distance": "Distance",
    "Angle Position": "Angle position",
    "Rotation": "Rotation",
    "Water Temperature": "Water temperature",
    "Soil Temperature": "Soil temperature",
    "Seismic Intensity": "Seismic Intensity",
    "Seismic Magnitude": "Seismic magnitude",
    "Ultraviolet": "Ultraviolet",
    "Electrical Resistivity": "Electrical resistivity",
    "Electrical Conductivity": "Electrical conductivity",
    "Loudness": "Loudness",
    "Moisture": "Moisture",
}

CC_ID_LABEL_TO_PROPERTY = {
    49: SENSOR_MULTILEVEL_CC_LABEL_TO_PROPERTY_NAME,
    113: NOTIFICATION_CC_LABEL_TO_PROPERTY_NAME,
}


@callback
def async_generate_migration_data(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    all_discovered_values: dict[str, ZwaveDiscoveryInfo],
) -> None:
    """Generate Z-Wave JS migration data."""
    migration_handler: LegacyZWaveMigration = get_legacy_zwave_migration(hass)
    migration_handler.generate_data(config_entry, all_discovered_values)


async def async_get_migration_data(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, dict[str, int | str | None]]:
    """Return Z-Wave JS migration data."""
    migration_handler: LegacyZWaveMigration = get_legacy_zwave_migration(hass)
    return await migration_handler.get_data(config_entry)


@singleton(LEGACY_ZWAVE_MIGRATION)
@callback
def get_legacy_zwave_migration(hass: HomeAssistant) -> LegacyZWaveMigration:
    """Return legacy Z-Wave migration handler."""
    return LegacyZWaveMigration(hass)


class LegacyZWaveMigration:
    """Handle the migration from zwave to zwave_js."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up migration instance."""
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, dict[str, dict[str, int | str | None]]] = {}

    async def load_data(self) -> None:
        """Load Z-Wave JS migration data."""
        stored = cast(dict, await self._store.async_load())
        if stored:
            self._data = stored

    @callback
    def save_data(
        self, data: dict[str, dict[str, dict[str, int | str | None]]]
    ) -> None:
        """Save Z-Wave JS migration data."""
        self._data.update(data)
        self._store.async_delay_save(self._data_to_save, STORAGE_WRITE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, dict[str, dict[str, int | str | None]]]:
        """Return data to save."""
        return self._data

    @callback
    def generate_data(
        self,
        config_entry: ConfigEntry,
        all_discovered_values: dict[str, ZwaveDiscoveryInfo],
    ) -> None:
        """Create zwave_js side migration data for a config entry."""
        data: dict[str, dict[str, int | str | None]] = {}
        ent_reg = async_get_entity_registry(self._hass)
        entity_entries = async_entries_for_config_entry(ent_reg, config_entry.entry_id)
        unique_entries = {entry.unique_id: entry for entry in entity_entries}
        dev_reg = async_get_device_registry(self._hass)

        for info in all_discovered_values.values():
            node = info.node
            primary_value = info.primary_value
            unique_id = get_unique_id(
                node.client.driver.controller.home_id, primary_value.value_id
            )
            if unique_id not in unique_entries:
                _LOGGER.debug("Missing entity entry for: %s", unique_id)
                continue
            entity_entry = unique_entries[unique_id]
            device_identifier = get_device_id(node.client, node)
            device_entry = dev_reg.async_get_device({device_identifier}, set())
            if not device_entry:
                _LOGGER.debug("Missing device entry for: %s", device_identifier)
                continue
            data[unique_id] = {
                "node_id": node.node_id,
                "endpoint_index": node.index,
                "command_class": primary_value.command_class,
                "value_property_name": primary_value.property_name,
                "value_property_key_name": primary_value.property_key_name,
                "value_id": primary_value.value_id,
                "device_id": device_entry.id,
                "domain": entity_entry.domain,
                "entity_id": entity_entry.entity_id,
                "unique_id": unique_id,
                "unit_of_measurement": entity_entry.unit_of_measurement,
            }

        _LOGGER.debug("Collected migration data: %s", data)

        self.save_data({config_entry.entry_id: data})

    async def get_data(
        self, config_entry: ConfigEntry
    ) -> dict[str, dict[str, int | str | None]]:
        """Return Z-Wave JS migration data for a config entry."""
        await self.load_data()
        data = self._data.get(config_entry.entry_id)
        return data or {}


@dataclass
class ValueID:
    """Class to represent a Value ID."""

    command_class: str
    endpoint: str
    property_: str
    property_key: str | None = None

    @staticmethod
    def from_unique_id(unique_id: str) -> ValueID:
        """
        Get a ValueID from a unique ID.

        This also works for Notification CC Binary Sensors which have their own unique ID
        format.
        """
        return ValueID.from_string_id(unique_id.split(".")[1])

    @staticmethod
    def from_string_id(value_id_str: str) -> ValueID:
        """Get a ValueID from a string representation of the value ID."""
        parts = value_id_str.split("-")
        property_key = parts[4] if len(parts) > 4 else None
        return ValueID(parts[1], parts[2], parts[3], property_key=property_key)

    def is_same_value_different_endpoints(self, other: ValueID) -> bool:
        """Return whether two value IDs are the same excluding endpoint."""
        return (
            self.command_class == other.command_class
            and self.property_ == other.property_
            and self.property_key == other.property_key
            and self.endpoint != other.endpoint
        )


@callback
def async_migrate_old_entity(
    hass: HomeAssistant,
    ent_reg: EntityRegistry,
    registered_unique_ids: set[str],
    platform: str,
    device: DeviceEntry,
    unique_id: str,
) -> None:
    """Migrate existing entity if current one can't be found and an old one exists."""
    # If we can find an existing entity with this unique ID, there's nothing to migrate
    if ent_reg.async_get_entity_id(platform, DOMAIN, unique_id):
        return

    value_id = ValueID.from_unique_id(unique_id)

    # Look for existing entities in the registry that could be the same value but on
    # a different endpoint
    existing_entity_entries: list[RegistryEntry] = []
    for entry in async_entries_for_device(ent_reg, device.id):
        # If entity is not in the domain for this discovery info or entity has already
        # been processed, skip it
        if entry.domain != platform or entry.unique_id in registered_unique_ids:
            continue

        try:
            old_ent_value_id = ValueID.from_unique_id(entry.unique_id)
        # Skip non value ID based unique ID's (e.g. node status sensor)
        except IndexError:
            continue

        if value_id.is_same_value_different_endpoints(old_ent_value_id):
            existing_entity_entries.append(entry)
            # We can return early if we get more than one result
            if len(existing_entity_entries) > 1:
                return

    # If we couldn't find any results, return early
    if not existing_entity_entries:
        return

    entry = existing_entity_entries[0]
    state = hass.states.get(entry.entity_id)

    if not state or state.state == STATE_UNAVAILABLE:
        async_migrate_unique_id(ent_reg, platform, entry.unique_id, unique_id)


@callback
def async_migrate_unique_id(
    ent_reg: EntityRegistry, platform: str, old_unique_id: str, new_unique_id: str
) -> None:
    """Check if entity with old unique ID exists, and if so migrate it to new ID."""
    if entity_id := ent_reg.async_get_entity_id(platform, DOMAIN, old_unique_id):
        _LOGGER.debug(
            "Migrating entity %s from old unique ID '%s' to new unique ID '%s'",
            entity_id,
            old_unique_id,
            new_unique_id,
        )
        try:
            ent_reg.async_update_entity(entity_id, new_unique_id=new_unique_id)
        except ValueError:
            _LOGGER.debug(
                (
                    "Entity %s can't be migrated because the unique ID is taken; "
                    "Cleaning it up since it is likely no longer valid"
                ),
                entity_id,
            )
            ent_reg.async_remove(entity_id)


@callback
def async_migrate_discovered_value(
    hass: HomeAssistant,
    ent_reg: EntityRegistry,
    registered_unique_ids: set[str],
    device: DeviceEntry,
    client: ZwaveClient,
    disc_info: ZwaveDiscoveryInfo,
) -> None:
    """Migrate unique ID for entity/entities tied to discovered value."""

    new_unique_id = get_unique_id(
        client.driver.controller.home_id,
        disc_info.primary_value.value_id,
    )

    # On reinterviews, there is no point in going through this logic again for already
    # discovered values
    if new_unique_id in registered_unique_ids:
        return

    # Migration logic was added in 2021.3 to handle a breaking change to the value_id
    # format. Some time in the future, the logic to migrate unique IDs can be removed.

    # 2021.2.*, 2021.3.0b0, and 2021.3.0 formats
    old_unique_ids = [
        get_unique_id(
            client.driver.controller.home_id,
            value_id,
        )
        for value_id in get_old_value_ids(disc_info.primary_value)
    ]

    if (
        disc_info.platform == "binary_sensor"
        and disc_info.platform_hint == "notification"
    ):
        for state_key in disc_info.primary_value.metadata.states:
            # ignore idle key (0)
            if state_key == "0":
                continue

            new_bin_sensor_unique_id = f"{new_unique_id}.{state_key}"

            # On reinterviews, there is no point in going through this logic again
            # for already discovered values
            if new_bin_sensor_unique_id in registered_unique_ids:
                continue

            # Unique ID migration
            for old_unique_id in old_unique_ids:
                async_migrate_unique_id(
                    ent_reg,
                    disc_info.platform,
                    f"{old_unique_id}.{state_key}",
                    new_bin_sensor_unique_id,
                )

            # Migrate entities in case upstream changes cause endpoint change
            async_migrate_old_entity(
                hass,
                ent_reg,
                registered_unique_ids,
                disc_info.platform,
                device,
                new_bin_sensor_unique_id,
            )
            registered_unique_ids.add(new_bin_sensor_unique_id)

        # Once we've iterated through all state keys, we are done
        return

    # Unique ID migration
    for old_unique_id in old_unique_ids:
        async_migrate_unique_id(
            ent_reg, disc_info.platform, old_unique_id, new_unique_id
        )

    # Migrate entities in case upstream changes cause endpoint change
    async_migrate_old_entity(
        hass, ent_reg, registered_unique_ids, disc_info.platform, device, new_unique_id
    )
    registered_unique_ids.add(new_unique_id)


@callback
def get_old_value_ids(value: ZwaveValue) -> list[str]:
    """Get old value IDs so we can migrate entity unique ID."""
    value_ids = []

    # Pre 2021.3.0 value ID
    command_class = value.command_class
    endpoint = value.endpoint or "00"
    property_ = value.property_
    property_key_name = value.property_key_name or "00"
    value_ids.append(
        f"{value.node.node_id}.{value.node.node_id}-{command_class}-{endpoint}-"
        f"{property_}-{property_key_name}"
    )

    endpoint = "00" if value.endpoint is None else value.endpoint
    property_key = "00" if value.property_key is None else value.property_key
    property_key_name = value.property_key_name or "00"

    value_id = (
        f"{value.node.node_id}-{command_class}-{endpoint}-"
        f"{property_}-{property_key}-{property_key_name}"
    )
    # 2021.3.0b0 and 2021.3.0 value IDs
    value_ids.extend([f"{value.node.node_id}.{value_id}", value_id])

    return value_ids
