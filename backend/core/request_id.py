"""Opaque request identifiers for safe client/server error correlation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Request, Response


REQUEST_ID_HEADER = "X-Request-ID"


async def attach_request_id(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach a server-generated ID; never trust a caller-provided value."""

    request_id = uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response
