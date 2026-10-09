"""Message and homework attachments straight from Librus to the browser.

`GET /api/librus_synergia/attachment/<device id>/<message id>/<attachment id>`
(Home Assistant login required) downloads one Wiadomości attachment and
streams it back as a file download. Nothing is written to Home Assistant's
disk, and the message isn't opened (marked read) in Librus. The companion
Messages card calls this when a file name is tapped.

`GET /api/librus_synergia/homework_attachment/<device id>/<attachment id>`
does the same for a homework assignment's file (the Homework checklist card),
and `GET /api/librus_synergia/school_file/<device id>/<file id>` for a school
document (the School documents card) - its Synergia link alone needs a
logged-in Synergia session, which the browser doesn't have.
"""

from __future__ import annotations

import logging
import re
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
SCHOOL_FILE_URL = f"/api/{DOMAIN}/school_file/{{device_id}}/{{file_id}}"

# Librus ids in the URL: letters, digits, "-" and "_" only. Anything else is
# answered 404 before a request is made - an id is pasted into Librus URLs
# by the library, so "../" or "?x=" must never get that far.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Control characters (a newline would split the Content-Disposition header).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _valid_ids(*ids: str) -> bool:
    return all(_ID_RE.match(value) for value in ids)


def _file_response(file: Any, fallback_name: str) -> web.Response:
    filename = _CONTROL_RE.sub("", file.filename or "").strip() or fallback_name
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
            # Served as a download, never sniffed into HTML/script by the
            # browser on Home Assistant's own origin.
            "X-Content-Type-Options": "nosniff",
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
        # Librus's own error text stays in the log: it can carry URLs and
        # session details that don't belong in a browser response.
        if getattr(err, "status_code", None) == 404:
            _LOGGER.debug("Attachment not found (%s): %s", request.path, err)
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Not found")
        _LOGGER.warning("Attachment download failed (%s): %s", request.path, err)
        return web.Response(status=HTTPStatus.BAD_GATEWAY, text="Librus: download failed")
    except ValueError as err:
        # The library raises ValueError for an answer it can't make sense
        # of (e.g. a sandbox key or redirect it doesn't recognise) - still
        # Librus's side, so a short 502 rather than a 500 with a traceback.
        _LOGGER.warning("Attachment download failed (%s): %s", request.path, err)
        return web.Response(
            status=HTTPStatus.BAD_GATEWAY, text="Librus: unexpected download response"
        )
    return _file_response(file, fallback_name)


class LibrusAttachmentView(HomeAssistantView):
    """Streams one message attachment to the signed-in Home Assistant user."""

    url = URL
    name = f"api:{DOMAIN}:attachment"
    requires_auth = True

    async def get(
        self, request: web.Request, device_id: str, message_id: str, attachment_id: str
    ) -> web.Response:
        if not _valid_ids(device_id, message_id, attachment_id):
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Not found")
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
        if not _valid_ids(device_id, attachment_id):
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Not found")
        return await _async_download(
            request,
            device_id,
            lambda c: c.async_download_homework_attachment(attachment_id),
            f"homework-file-{attachment_id}",
        )


class LibrusSchoolFileView(HomeAssistantView):
    """Streams one school document (SchoolFiles)."""

    url = SCHOOL_FILE_URL
    name = f"api:{DOMAIN}:school_file"
    requires_auth = True

    async def get(self, request: web.Request, device_id: str, file_id: str) -> web.Response:
        if not _valid_ids(device_id, file_id):
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Not found")
        return await _async_download(
            request,
            device_id,
            lambda c: c.async_download_school_file(file_id),
            f"school-file-{file_id}",
        )


def async_register_attachment_view(hass: HomeAssistant) -> None:
    """Register the view once per Home Assistant run (views can't be
    unregistered; with no Librus entry loaded it answers 404)."""
    if hass.data.get(f"{DOMAIN}_attachment_view"):
        return
    hass.http.register_view(LibrusAttachmentView())
    hass.http.register_view(LibrusHomeworkAttachmentView())
    hass.http.register_view(LibrusSchoolFileView())
    hass.data[f"{DOMAIN}_attachment_view"] = True
