"""Grade-average history for Home Assistant's long-term statistics.

The average sensors only have history from the day the integration was
installed. This rebuilds the whole school year instead: for every day
since the first grade, the average of the grades added up to that day -
overall and per subject - written as external statistics
(`librus_synergia:<entry>_average[_<subject>]`). A Statistics graph card
can then chart the year, picked by name ("Ola Kowalska - średnia
Matematyka").

The statistics are rewritten whenever the grades change and once a day
(so today gets its point); writing the same days again just replaces
them. Nothing is fetched from Librus for this.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from librus_synergia.models import GradeData, LibrusData

from .ai_summary import _day
from .const import AVERAGE_MODE_ARITHMETIC, CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE, DOMAIN
from .coordinator import LibrusDataUpdateCoordinator, calculate_average

_LOGGER = logging.getLogger(__name__)

# A school year is ~300 days; anything older than this isn't this year's.
MAX_DAYS = 400


def daily_averages(
    grades: list[GradeData],
    categories: dict[int, Any],
    today: date,
    *,
    subject_id: Any = None,
    weighted: bool = True,
) -> list[tuple[date, float]]:
    """(day, average of the grades added up to and including that day),
    from the first grade's day to `today`; days with no average yet are
    left out."""
    dated = sorted(
        ((d, g) for g in grades if (d := _day(g.add_date)) is not None and d <= today),
        key=lambda item: item[0],
    )
    if subject_id is not None:
        dated = [(d, g) for d, g in dated if g.subject_id == subject_id]
    if not dated:
        return []
    first = max(dated[0][0], today - timedelta(days=MAX_DAYS))
    out: list[tuple[date, float]] = []
    included: list[GradeData] = []
    index = 0
    day = first
    average: float | None = None
    while day <= today:
        changed = False
        while index < len(dated) and dated[index][0] <= day:
            included.append(dated[index][1])
            index += 1
            changed = True
        if changed:
            average = calculate_average(included, categories, weighted=weighted)
        if average is not None:
            out.append((day, average))
        day += timedelta(days=1)
    return out


def _metadata(statistic_id: str, name: str) -> dict[str, Any]:
    """StatisticMetaData for this Home Assistant: `mean_type`/`unit_class`
    where the recorder knows them, the older `has_mean` otherwise."""
    from homeassistant.components.recorder import models  # noqa: PLC0415

    meta: dict[str, Any] = {
        "has_sum": False,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": None,
    }
    fields = getattr(models.StatisticMetaData, "__annotations__", {})
    mean_type = getattr(models, "StatisticMeanType", None)
    if "mean_type" in fields and mean_type is not None:
        meta["mean_type"] = mean_type.ARITHMETIC
    else:
        meta["has_mean"] = True
    if "unit_class" in fields:
        meta["unit_class"] = None
    return meta


class LibrusAverageHistory:
    """Keeps one entry's average statistics in step with its grades."""

    def __init__(self, hass: HomeAssistant, coordinator: LibrusDataUpdateCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._signature: Any = None
        self._unsub: Callable[[], None] | None = None

    @property
    def _prefix(self) -> str:
        entry = self._coordinator.config_entry
        return f"{DOMAIN}:{slugify(entry.entry_id if entry else 'librus')}_average"

    @callback
    def async_start(self) -> None:
        if "recorder" not in self._hass.config.components:
            _LOGGER.debug("Recorder not loaded - no grade-average statistics")
            return
        self._unsub = self._coordinator.async_add_listener(self._async_update)
        self._async_update()

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _weighted(self) -> bool:
        entry = self._coordinator.config_entry
        mode = entry.options.get(CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE) if entry else None
        return mode != AVERAGE_MODE_ARITHMETIC

    @callback
    def _async_update(self) -> None:
        data = self._coordinator.data
        if data is None or not data.grades:
            return
        today = dt_util.now().date()
        weighted = self._weighted()
        signature = (
            today,
            weighted,
            tuple(sorted((g.id, g.value, g.add_date or "", g.category_id or 0) for g in data.grades)),
        )
        if signature == self._signature:
            return
        self._signature = signature
        try:
            self._async_write(data, today, weighted)
        except Exception:  # noqa: BLE001 - statistics are a bonus, never break updates
            _LOGGER.exception("Could not write grade-average statistics")

    @callback
    def _async_write(self, data: LibrusData, today: date, weighted: bool) -> None:
        from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
            async_add_external_statistics,
        )

        student = data.me.display_name
        series: list[tuple[str, str, Any]] = [(self._prefix, f"{student} - średnia", None)]
        for subject_id in sorted({g.subject_id for g in data.grades if g.subject_id is not None}, key=str):
            subject = data.subjects.get(subject_id) or str(subject_id)
            series.append(
                (
                    f"{self._prefix}_{slugify(str(subject_id))}",
                    f"{student} - średnia {subject}",
                    subject_id,
                )
            )
        for statistic_id, name, subject_id in series:
            points = daily_averages(
                data.grades, data.grade_categories, today, subject_id=subject_id, weighted=weighted
            )
            if not points:
                continue
            rows = [
                {
                    "start": dt_util.start_of_local_day(day),
                    "mean": value,
                    "min": value,
                    "max": value,
                }
                for day, value in points
            ]
            async_add_external_statistics(self._hass, _metadata(statistic_id, name), rows)
