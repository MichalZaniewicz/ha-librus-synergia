"""Homework (zadania domowe) as a Home Assistant to-do list.

Items come from Librus's HomeWorkAssignments; ticking one off is stored in
Home Assistant only (Librus has no "done" flag for a parent or student to
set), so it is shared by every device and person using this Home
Assistant, and survives restarts. Items can't be added, renamed or deleted
here: the list mirrors Librus.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import LibrusConfigEntry, librus_device_info
from .ai_summary import _cut, _day
from .const import DOMAIN
from .coordinator import LibrusDataUpdateCoordinator, infer_subject_id, teacher_subject_ids

STORAGE_VERSION = 1
# Ticked-off homework stays on the list this long after its due date.
KEEP_DONE_DAYS = 14


class LibrusHomeworkTodoList(CoordinatorEntity[LibrusDataUpdateCoordinator], TodoListEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "homework"
    _attr_icon = "mdi:notebook-check-outline"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_homework"
        self._attr_device_info = librus_device_info(entry)
        self._store: Store[dict[str, Any]] = Store(
            coordinator.hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.homework_done"
        )
        self._done: set[str] = set()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        stored = await self._store.async_load() or {}
        self._done = {str(uid) for uid in stored.get("done", [])}

    @property
    def todo_items(self) -> list[TodoItem] | None:
        data = self.coordinator.data
        if data is None:
            return None
        today = dt_util.now().date()
        by_teacher = teacher_subject_ids(data.timetable)
        items: list[TodoItem] = []
        for hw in sorted(data.homework_assignments, key=lambda h: h.due_date or ""):
            uid = str(hw.id)
            done = uid in self._done
            due = _day(hw.due_date)
            if done and due is not None and due < today - timedelta(days=KEEP_DONE_DAYS):
                continue
            subject_id = infer_subject_id(hw.teacher_id, by_teacher)
            subject = data.subjects.get(subject_id) if subject_id is not None else None
            title = _cut(hw.topic, 120) or _cut(hw.text, 80) or "Zadanie domowe"
            items.append(
                TodoItem(
                    uid=uid,
                    summary=f"{subject}: {title}" if subject else title,
                    status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION,
                    due=due,
                    description=_cut(hw.text, 2000),
                )
            )
        return items

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Only the done/not-done tick is kept; Librus owns the rest."""
        data = self.coordinator.data
        known = {str(hw.id) for hw in (data.homework_assignments if data else [])}
        if item.uid not in known:
            raise HomeAssistantError("This homework is no longer in Librus.")
        if item.status == TodoItemStatus.COMPLETED:
            self._done.add(item.uid)
        else:
            self._done.discard(item.uid)
        # Forget ticks for homework Librus no longer lists.
        self._done &= known
        await self._store.async_save({"done": sorted(self._done)})
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: LibrusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LibrusHomeworkTodoList(entry.runtime_data, entry)])
