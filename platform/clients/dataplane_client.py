"""
High-level DataPlane client for ExaMLOps.

Wraps the raw dataplane.client.Connection with a typed, async-generator-
based API so callers never touch raw Cap'n'Proto payloads directly.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeVar

from dataplane.client import Connection as _RawConnection

T = TypeVar("T")


class Connection:
    """Typed dataplane connection.

    Usage::

        conn = Connection(host, port)
        await conn.connect()

        # pub/sub
        async for job in conn.subscribe(topic_uuid, HpcJobV1):
            ...

        await conn.publish(topic_uuid, HpcInferenceResV1(...))

        # req/res (server side)
        await conn.serve(service_uuid, HpcJobV1, async_handler)

        # req/res (client side)
        res = await conn.request(service_uuid, req, HpcInferenceResV1)
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._conn: _RawConnection | None = None

    async def connect(self) -> None:
        self._conn = await _RawConnection.connect(self._host, self._port)

    async def subscribe(self, topic: uuid.UUID, msg_class: type[T]) -> AsyncGenerator[T, None]:
        """Subscribe to *topic* and yield decoded messages of *msg_class*.

        Raises EOFError if the server closes the connection.
        """
        assert self._conn is not None, "call connect() first"
        await self._conn.subscribe(topic)
        while True:
            raw = await self._conn.read_msg()
            if raw is None:
                raise EOFError("DataPlane connection closed")
            yield msg_class.from_capnp(raw)

    async def publish(self, topic: uuid.UUID, msg: Any) -> None:
        """Publish *msg* (must implement to_capnp()) to *topic*."""
        assert self._conn is not None, "call connect() first"
        await self._conn.publish(topic, msg.to_capnp())

    async def serve(
        self,
        service: uuid.UUID,
        req_class: type[T],
        handler: Callable[[T], Awaitable[Any]],
    ) -> None:
        """Register *service* and dispatch each incoming request to *handler*.

        Runs until cancelled or the connection is closed. *handler* receives a
        decoded *req_class* instance and must return an object implementing to_capnp().
        Raises EOFError if the server closes the connection.
        """
        assert self._conn is not None, "call connect() first"
        await self._conn.register(service)
        while True:
            raw = await self._conn.read_msg()
            if raw is None:
                raise EOFError("DataPlane connection closed")
            req = req_class.from_capnp(raw)
            res = await handler(req)
            await self._conn.respond_to(raw, res.to_capnp())

    async def request(self, service: uuid.UUID, req: Any, res_class: type[T]) -> T:
        """Send *req* to *service* and return a decoded *res_class* response."""
        assert self._conn is not None, "call connect() first"
        raw = await self._conn.request(service, req.to_capnp())
        return res_class.from_capnp(raw)

    async def close(self) -> None:
        pass  # pycapnp manages the stream lifecycle
