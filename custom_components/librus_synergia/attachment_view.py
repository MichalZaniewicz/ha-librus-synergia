"""Message and homework attachments straight from Librus to the browser.

`GET /api/librus_synergia/attachment/<device id>/<message id>/<attachment id>`
(Home Assistant login required) downloads one Wiadomości attachment and
streams it back as a file download. Nothing is written to Home Assistant's
disk, and the message isn't opened (marked read) in Librus. The companion
Messages card calls this when a file name is tapped.

`GET /api/librus_synergia/homework_attachment/<device id>/<attachment id>`
does the same for a homework assignment's file (the Homework checklist card).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from librus_synergia import LibrusError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL = f"/api/{DOMAIN}/attachment/{{device_id}}/{{message_id}}/{{attachment_id}}"
HOMEWORK_URL = f"/api/{DOMAIN}/homework_attachment/{{device_id}}/{{attachment_id}}"


def _file_response(file: Any, fallback_name: str) -> web.Response:
    filename = file.filename or fallback_name
    ascii_name = filename.encode("ascii", "ignore").decode() or "attachment"
    ascii_name = ascii_name.replace('"', "").replace("\\", "")
    return web.Response(
        body=file.content,
        content_type=file.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "no-store",
        },
    )


async def _async_download(
    request: web.Request,
    device_id: str,
    download: Callable[[Any], Awaitable[Any]],
    fallback_name: str,
) -> web.Response:
    # Imported here: services.py imports the coordinator, which this
    # module must not pull in at import time of __init__.
    from .services import resolve_coordinator  # noqa: PLC0415

    hass: HomeAssistant = request.app["hass"]
    try:
        coordinator = resolve_coordinator(hass, device_id)
    except HomeAssistantError as err:
        return web.Response(status=HTTPStatus.NOT_FOUND, text=str(err))
    try:
        file = await download(coordinator)
    except LibrusError as err:
        _LOGGER.warning("Attachment download failed (%s): %s", request.path, err)
        return web.Response(status=HTTPStatus.BAD_GATEWAY, text=f"Librus: {err}")
    return _file_response(file, fallback_name)


class LibrusAttachmentView(HomeAssistantView):
    """Streams one message attachment to the signed-in Home Assistant user."""

    url = URL
    name = f"api:{DOMAIN}:attachment"
    requires_auth = True

    async def get(
        self, request: web.Request, device_id: str, message_id: str, attachment_id: str
    ) -> web.Response:
        return await _async_download(
            request,
            device_id,
            lambda c: c.async_download_attachment(attachment_id, message_id),
            f"attachment-{attachment_id}",
        )


class LibrusHomeworkAttachmentView(HomeAssistantView):
    """Streams one homework-assignment attachment."""

    url = HOMEWORK_URL
    name = f"api:{DOMAIN}:homework_attachment"
    requires_auth = True

    async def get(self, request: web.Request, device_id: str, attachment_id: str) -> web.Response:
        return await _async_download(
            request,
            device_id,
            lambda c: c.async_download_homework_attachment(attachment_id),
            f"homework-file-{attachment_id}",
        )


def async_register_attachment_view(hass: HomeAssistant) -> None:
    """Register the view once per Home Assistant run (views can't be
    unregistered; with no Librus entry loaded it answers 404)."""
    if hass.data.get(f"{DOMAIN}_attachment_view"):
        return
    hass.http.register_view(LibrusAttachmentView())
    hass.http.register_view(LibrusHomeworkAttachmentView())
    hass.data[f"{DOMAIN}_attachment_view"] = True
