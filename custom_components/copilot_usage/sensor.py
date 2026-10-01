"""Sensors for Copilot Usage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BillingTotals, BillingUsage, Quota, QuotaUsage
from .const import AUTH_BILLING, DOMAIN
from .coordinator import CopilotUsageConfigEntry, CopilotUsageCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CopilotUsageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors."""
    coordinator = entry.runtime_data
    if coordinator.auth_type == AUTH_BILLING:
        async_add_entities(BillingSensor(coordinator, d) for d in BILLING_SENSORS)
        return

    async_add_entities([PlanSensor(coordinator), ResetSensor(coordinator)])
    known: set[str] = set()

    @callback
    def _add_new_quotas() -> None:
        data = coordinator.data
        if not isinstance(data, QuotaUsage):
            return
        new: list[SensorEntity] = []
        for key, quota in data.quotas.items():
            if key in known or quota.unlimited:
                continue
            known.add(key)
            new += [
                QuotaPercentSensor(coordinator, key, quota.name),
                QuotaRemainingSensor(coordinator, key, quota.name),
            ]
        if new:
            async_add_entities(new)

    _add_new_quotas()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_quotas))


class CopilotEntity(CoordinatorEntity[CopilotUsageCoordinator]):
    """Base entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CopilotUsageCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        billing = coordinator.auth_type == AUTH_BILLING
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="GitHub",
            model="Billing API" if billing else "Copilot",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/settings/billing"
            if billing
            else "https://github.com/settings/copilot/features",
        )

    @property
    def _usage(self) -> QuotaUsage | None:
        data = self.coordinator.data
        return data if isinstance(data, QuotaUsage) else None


class _QuotaEntity(CopilotEntity):
    def __init__(self, coordinator: CopilotUsageCoordinator, key: str, suffix: str) -> None:
        super().__init__(coordinator, f"{key}_{suffix}")
        self._key = key

    @property
    def _quota(self) -> Quota | None:
        usage = self._usage
        return usage.quotas.get(self._key) if usage else None

    @property
    def available(self) -> bool:
        return super().available and self._quota is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        q = self._quota
        if not q:
            return None
        attrs: dict[str, Any] = {
            "entitlement": q.entitlement,
            "remaining": q.remaining,
            "used": q.used,
            "unlimited": q.unlimited,
        }
        attrs.update(q.extra)
        return attrs


class QuotaPercentSensor(_QuotaEntity, SensorEntity):
    """Percent of a monthly quota used."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator: CopilotUsageCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key, "percent_used")
        self._attr_name = f"{name} used"

    @property
    def native_value(self) -> float | None:
        return self._quota.percent_used if self._quota else None


class QuotaRemainingSensor(_QuotaEntity, SensorEntity):
    """Remaining units in a monthly quota."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: CopilotUsageCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key, "remaining")
        self._attr_name = f"{name} remaining"
        self._attr_native_unit_of_measurement = "requests"

    @property
    def native_value(self) -> float | None:
        return self._quota.remaining if self._quota else None


class PlanSensor(CopilotEntity, SensorEntity):
    """Copilot plan."""

    _attr_icon = "mdi:card-account-details-outline"
    _attr_name = "Plan"

    def __init__(self, coordinator: CopilotUsageCoordinator) -> None:
        super().__init__(coordinator, "plan")

    @property
    def native_value(self) -> str | None:
        return self._usage.plan if self._usage else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._usage:
            return None
        return {"login": self._usage.login, "sku": self._usage.sku}


class ResetSensor(CopilotEntity, SensorEntity):
    """When monthly quotas reset."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-refresh"
    _attr_name = "Quota reset"

    def __init__(self, coordinator: CopilotUsageCoordinator) -> None:
        super().__init__(coordinator, "quota_reset")

    @property
    def native_value(self) -> datetime | None:
        return self._usage.reset_at if self._usage else None


# --------------------------------------------------------------------- billing


@dataclass(frozen=True, kw_only=True)
class BillingSensorDescription(SensorEntityDescription):
    """Billing sensor description."""

    totals_fn: Callable[[BillingUsage], BillingTotals | None]
    value_fn: Callable[[BillingTotals], float]
    period: str


def _billing_descriptions() -> tuple[BillingSensorDescription, ...]:
    out: list[BillingSensorDescription] = []
    for kind, label, unit, icon in (
        ("premium", "Premium requests", "requests", "mdi:star-four-points-outline"),
        ("credits", "AI credits", "credits", "mdi:alpha-c-circle-outline"),
    ):
        for period, suffix in (("month", "this month"), ("today", "today")):
            attr = f"{kind}_{period}"
            out.append(
                BillingSensorDescription(
                    key=f"{kind}_quantity_{period}",
                    name=f"{label} {suffix}",
                    icon=icon,
                    native_unit_of_measurement=unit,
                    state_class=SensorStateClass.TOTAL,
                    suggested_display_precision=0,
                    totals_fn=lambda d, a=attr: getattr(d, a),
                    value_fn=lambda t: round(t.quantity, 2),
                    period=period,
                )
            )
            out.append(
                BillingSensorDescription(
                    key=f"{kind}_billed_{period}",
                    name=f"{label} billed {suffix}",
                    device_class=SensorDeviceClass.MONETARY,
                    native_unit_of_measurement="USD",
                    state_class=SensorStateClass.TOTAL,
                    suggested_display_precision=2,
                    totals_fn=lambda d, a=attr: getattr(d, a),
                    value_fn=lambda t: t.net_amount,
                    period=period,
                )
            )
    return tuple(out)


BILLING_SENSORS = _billing_descriptions()


class BillingSensor(CopilotEntity, SensorEntity):
    """Copilot billing usage sensor."""

    entity_description: BillingSensorDescription

    def __init__(
        self, coordinator: CopilotUsageCoordinator, description: BillingSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def _totals(self) -> BillingTotals | None:
        data = self.coordinator.data
        if not isinstance(data, BillingUsage):
            return None
        return self.entity_description.totals_fn(data)

    @property
    def available(self) -> bool:
        return super().available and self._totals is not None

    @property
    def native_value(self) -> float | None:
        totals = self._totals
        return self.entity_description.value_fn(totals) if totals else None

    @property
    def last_reset(self) -> datetime | None:
        data = self.coordinator.data
        if not isinstance(data, BillingUsage):
            return None
        return data.month_start if self.entity_description.period == "month" else data.day_start

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        totals = self._totals
        if not totals:
            return None
        return {"gross_amount": totals.gross_amount, "by_model": totals.by_model}
