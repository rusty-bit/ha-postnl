"""Sensor for PostNL packages."""
import logging
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from . import DOMAIN
from .coordinator import PostNLCoordinator
from .structs.package import Package

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up the PostNL sensor platform."""
    _LOGGER.debug("Setting up PostNL sensors")

    coordinator = PostNLCoordinator(hass)
    await coordinator.async_config_entry_first_refresh()

    userinfo = hass.data[DOMAIN][entry.entry_id].get("userinfo", {})
    if not userinfo:
        _LOGGER.error("No userinfo found for PostNL entry")
        return

    _LOGGER.debug("Userinfo loaded: %s", userinfo)

    async_add_entities([
        PostNLDelivery(
            coordinator=coordinator,
            postnl_userinfo=userinfo,
            unique_id=userinfo.get('account_id') + "_" + "delivery",
            name="PostNL_delivery"
        ),
        PostNLDelivery(
            coordinator=coordinator,
            postnl_userinfo=userinfo,
            name="PostNL_distribution",
            unique_id=userinfo.get('account_id') + "_" + "distribution",
            receiver=False
        )
    ])
    _LOGGER.debug("PostNL summary sensors added")

    known_package_keys: set[str] = set()

    @callback
    def _async_add_package_sensors() -> None:
        """Add a per-package sensor for any package not yet tracked."""
        if not coordinator.data:
            return
        new_entities: list[PostNLPackageSensor] = []
        for data_key, receiver in (("receiver", True), ("sender", False)):
            for package in coordinator.data.get(data_key, []):
                sensor_key = f"{data_key}_{package.key}"
                if sensor_key not in known_package_keys:
                    known_package_keys.add(sensor_key)
                    new_entities.append(
                        PostNLPackageSensor(
                            coordinator=coordinator,
                            postnl_userinfo=userinfo,
                            package_key=package.key,
                            package_name=package.name,
                            receiver=receiver,
                        )
                    )
        if new_entities:
            _LOGGER.debug("Adding %d new package sensor(s)", len(new_entities))
            async_add_entities(new_entities)

    # Create sensors for packages already present on first load.
    _async_add_package_sensors()

    # Create sensors for packages that appear in future updates.
    entry.async_on_unload(coordinator.async_add_listener(_async_add_package_sensors))

    _LOGGER.debug("PostNL sensors setup complete")


class PostNLDelivery(CoordinatorEntity, Entity):
    def __init__(self, coordinator, postnl_userinfo, unique_id, name, receiver: bool = True):
        """Initialize the PostNL summary sensor."""
        super().__init__(coordinator, context=name)
        self.postnl_userinfo = postnl_userinfo
        self._unique_id = unique_id
        self._name: str = name
        self._attributes: dict[str, list[Package]] = {
            'enroute': [],
            'delivered': [],
        }
        self._state = None
        self.receiver: bool = receiver
        self.handle_coordinator_data()

    @property
    def unique_id(self) -> str | None:
        return self._unique_id

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.postnl_userinfo.get('account_id'))},
            name=self.postnl_userinfo.get('email'),
            manufacturer="PostNL",
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return 'packages'

    @property
    def extra_state_attributes(self):
        return self._attributes

    @property
    def icon(self):
        return "mdi:package-variant-closed"

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug('Updating sensor %s', self.name)
        self.handle_coordinator_data()
        self.async_write_ha_state()

    def handle_coordinator_data(self):
        self._attributes['delivered'] = []
        self._attributes['enroute'] = []

        if self.receiver:
            coordinator_data = self.coordinator.data['receiver']
        else:
            coordinator_data = self.coordinator.data['sender']

        for package in coordinator_data:
            if package.delivered:
                self._attributes['delivered'].append(vars(package))
            else:
                self._attributes['enroute'].append(vars(package))

        self._state = len(self._attributes['enroute'])


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string returned by the PostNL API."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class PostNLPackageSensor(CoordinatorEntity, SensorEntity):
    """A sensor that represents a single PostNL package.

    State is the expected (or planned) delivery datetime so that HA
    time-based automations can trigger directly on delivery windows.
    All delivery window fields are exposed as attributes.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:package-variant-closed"

    def __init__(
        self,
        coordinator: PostNLCoordinator,
        postnl_userinfo: dict,
        package_key: str,
        package_name: str,
        receiver: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self._package_key = package_key
        self._receiver = receiver
        self.postnl_userinfo = postnl_userinfo
        self._attr_unique_id = (
            f"{postnl_userinfo.get('account_id')}_package_{package_key}"
        )
        self._attr_name = f"PostNL {package_name}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.postnl_userinfo.get('account_id'))},
            name=self.postnl_userinfo.get('email'),
            manufacturer="PostNL",
        )

    def _get_package(self) -> Package | None:
        if not self.coordinator.data:
            return None
        data_key = "receiver" if self._receiver else "sender"
        return next(
            (p for p in self.coordinator.data.get(data_key, []) if p.key == self._package_key),
            None,
        )

    @property
    def available(self) -> bool:
        return self._get_package() is not None

    @property
    def native_value(self) -> datetime | None:
        package = self._get_package()
        if package is None:
            return None
        if package.delivered:
            return _parse_datetime(package.delivery_date)
        return (
            _parse_datetime(package.expected_datetime)
            or _parse_datetime(package.planned_date)
        )

    @property
    def extra_state_attributes(self) -> dict:
        package = self._get_package()
        if package is None:
            return {}
        return {
            "name": package.name,
            "url": package.url,
            "shipment_type": package.shipment_type,
            "status_message": package.status_message,
            "delivered": package.delivered,
            "delivery_date": package.delivery_date,
            "delivery_address_type": package.delivery_address_type,
            "planned_date": package.planned_date,
            "planned_from": package.planned_from,
            "planned_to": package.planned_to,
            "expected_datetime": package.expected_datetime,
            "expected_from": package.expected_from,
            "expected_to": package.expected_to,
            "last_update": package.last_update,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug("Updating package sensor %s", self._package_key)
        self.async_write_ha_state()
