"""Message attachments straight from Librus to the browser.

`GET /api/librus_synergia/attachment/<device id>/<message id>/<attachment id>`
(Home Assistant login required) downloads one Wiadomości attachment and
streams it back as a file download. Nothing is written to Home Assistant's
disk, and the message isn't opened (marked read) in Librus. The companion
Messages card calls this when a file name is tapped.
"""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from librus_synergia import LibrusError

from .const import DOMAIN

URL = f"/api/{DOMAIN}/attachment/{{device_id}}/{{message_id}}/{{attachment_id}}"


class LibrusAttachmentView(HomeAssistantView):
    """Streams one message attachment to the signed-in Home Assistant user."""

    url = URL
    name = f"api:{DOMAIN}:attachment"
    requires_auth = True

    async def get(
        self, request: web.Request, device_id: str, message_id: str, attachment_id: str
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
            file = await coordinator.async_download_attachment(attachment_id, message_id)
        except LibrusError as err:
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text=f"Librus: {err}")
        filename = file.filename or f"attachment-{attachment_id}"
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


def async_register_attachment_view(hass: HomeAssistant) -> None:
    """Register the view once per Home Assistant run (views can't be
    unregistered; with no Librus entry loaded it answers 404)."""
    if hass.data.get(f"{DOMAIN}_attachment_view"):
        return
    hass.http.register_view(LibrusAttachmentView())
    hass.data[f"{DOMAIN}_attachment_view"] = True
