"""Compatibility boundary for the optional ``lark-channel-sdk`` dependency.

The project keeps the workaround here instead of changing the installed SDK.
It is intentionally narrow: one private SDK loop is isolated per channel and
the WebSocket data-frame dispatcher accepts both event and interactive
callback frames.  The boundary can be removed once the upstream SDK carries
both fixes.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, Protocol, cast

_SDK_PATCH_LOCK = threading.RLock()


class _Channel(Protocol):
    def on(self, name: str, handler: Any) -> Any: ...

    async def connect_until_ready(
        self,
        *,
        timeout: float | None = 30.0,  # noqa: ASYNC109
    ) -> None: ...

    async def disconnect(self) -> None: ...

    async def update_card(self, message_id: str, card: dict[str, Any]) -> Any: ...

    def schedule(self, coro: Any) -> Any: ...


class _ChannelModule(Protocol):
    WSClient: type[Any]
    FeishuChannel: Callable[..., _Channel]


class _WsModule(Protocol):
    loop: asyncio.AbstractEventLoop | None
    _get_by_key: Callable[[Any, Any], Any]
    HEADER_MESSAGE_ID: Any
    HEADER_TRACE_ID: Any
    HEADER_SUM: Any
    HEADER_SEQ: Any
    HEADER_TYPE: Any
    HEADER_BIZ_RT: Any
    MessageType: Any
    logger: Any
    time: Any
    Response: Any
    base64: Any
    JSON: Any
    UTF_8: str


def _sdk_attr(obj: Any, name: str) -> Any:
    """Read a private member from the untyped optional SDK boundary."""
    return getattr(obj, name)


def create_channel(app_id: str, app_secret: str) -> CompatibleFeishuChannel:
    return CompatibleFeishuChannel(app_id=app_id, app_secret=app_secret)


class CompatibleFeishuChannel:
    """Small delegating facade that isolates SDK lifecycle quirks."""

    def __init__(self, *, app_id: str, app_secret: str) -> None:
        ws_module = cast(_WsModule, importlib.import_module("lark_channel.ws.client"))
        channel_module = cast(
            _ChannelModule,
            importlib.import_module("lark_channel.channel.channel"),
        )
        base_ws_client = channel_module.WSClient

        self._ws_module = ws_module
        self._channel_module = channel_module
        self._previous_loop = getattr(ws_module, "loop", None)
        self._sdk_loop = asyncio.new_event_loop()
        # lark-channel-sdk 1.2.0 resolves this module global from Client.start,
        # so it must be an idle loop that is not the project's asyncio.run loop.
        ws_module.loop = self._sdk_loop
        self._ws_client_class = _build_card_compatible_ws_client(
            base_ws_client, ws_module
        )
        self._channel = channel_module.FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            transport="ws",
        )
        self._closed = False

    def on(self, name: str, handler: Any) -> Any:
        return self._channel.on(name, handler)

    # SDK compatibility contract exposes a timeout parameter.
    async def connect_until_ready(
        self,
        *,
        timeout: float | None = 30.0,  # noqa: ASYNC109
    ) -> None:
        # FeishuChannel.start() looks up WSClient from its module at execution
        # time. Keep the replacement installed only for that construction.
        with _SDK_PATCH_LOCK:
            original = self._channel_module.WSClient
            self._channel_module.WSClient = self._ws_client_class
            try:
                await self._channel.connect_until_ready(timeout=timeout)
            finally:
                self._channel_module.WSClient = original

    async def disconnect(self) -> None:
        try:
            await self._channel.disconnect()
        finally:
            self._restore_sdk_loop()

    async def update_card(self, message_id: str, card: dict[str, Any]) -> Any:
        return await self._channel.update_card(message_id, card)

    def schedule(self, coro: Any) -> Any:
        return self._channel.schedule(coro)

    @property
    def sdk_loop(self) -> asyncio.AbstractEventLoop:
        return self._sdk_loop

    def _restore_sdk_loop(self) -> None:
        if self._closed:
            return
        if getattr(self._ws_module, "loop", None) is self._sdk_loop:
            self._ws_module.loop = self._previous_loop
        if not self._sdk_loop.is_running() and not self._sdk_loop.is_closed():
            self._sdk_loop.close()
        self._closed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._channel, name)


def _build_card_compatible_ws_client(
    base_cls: type[Any], ws_module: _WsModule
) -> type[Any]:
    """Create a version-local WS client with the missing CARD dispatch branch."""

    # lark-channel-sdk is optional and untyped; this is the single dynamic
    # subclass boundary where preserving the SDK's inheritance semantics is
    # preferable to rebuilding the class with type().
    class CardCompatibleWSClient(  # pyright: ignore[reportUntypedBaseClass]
        base_cls
    ):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # The SDK constructs this object in an executor thread. Setting the
            # loop there prevents asyncio primitives from binding to a missing
            # executor-thread loop or to the caller's running loop.
            asyncio.set_event_loop(ws_module.loop)
            super().__init__(  # pyright: ignore[reportUnknownMemberType]
                *args, **kwargs
            )

        async def _handle_data_frame(self, frame: Any) -> None:
            # This mirrors lark-channel-sdk's implementation, with CARD treated
            # like EVENT so the registered dispatcher can parse the payload and
            # the platform receives the normal acknowledgement frame.
            hs = frame.headers
            get_by_key = cast(
                Callable[[Any, Any], Any],
                _sdk_attr(cast(Any, ws_module), "_get_by_key"),
            )
            msg_id = get_by_key(hs, ws_module.HEADER_MESSAGE_ID)
            trace_id = get_by_key(hs, ws_module.HEADER_TRACE_ID)
            sum_ = get_by_key(hs, ws_module.HEADER_SUM)
            seq = get_by_key(hs, ws_module.HEADER_SEQ)
            type_ = get_by_key(hs, ws_module.HEADER_TYPE)

            payload = frame.payload
            sum_value = int(sum_)
            seq_value = int(seq)
            if sum_value > 1:
                combine = cast(
                    Callable[[Any, Any, Any, bytes], bytes | None],
                    _sdk_attr(cast(Any, self), "_combine"),
                )
                payload = combine(msg_id, sum_value, seq_value, payload)
                if payload is None:
                    return

            message_type = ws_module.MessageType(type_)
            ws_module.logger.debug(
                cast(Callable[..., str], _sdk_attr(cast(Any, self), "_fmt_log"))(
                    "receive message, message_type: {}, message_id: {}, "
                    "trace_id: {}, payload_len: {}",
                    message_type.value,
                    msg_id,
                    trace_id,
                    len(payload),
                )
            )

            if message_type not in {
                ws_module.MessageType.EVENT,
                ws_module.MessageType.CARD,
            }:
                return

            response = ws_module.Response(code=HTTPStatus.OK)
            try:
                start = round(ws_module.time.time() * 1000)
                event_handler = _sdk_attr(cast(Any, self), "_event_handler")
                result = event_handler._do_without_validation(payload)
                end = round(ws_module.time.time() * 1000)
                header = hs.add()
                header.key = ws_module.HEADER_BIZ_RT
                header.value = str(end - start)
                if result is not None:
                    response.data = ws_module.base64.b64encode(
                        ws_module.JSON.marshal(result).encode(ws_module.UTF_8)
                    )
            except Exception as exc:  # noqa: BLE001
                # Keep the SDK's frame acknowledgement alive even when the
                # optional handler raises an implementation-specific error.
                ws_module.logger.error(
                    cast(Callable[..., str], _sdk_attr(cast(Any, self), "_fmt_log"))(
                        "handle message failed, message_type: {}, message_id: {}, "
                        "trace_id: {}, err: {}",
                        message_type.value,
                        msg_id,
                        trace_id,
                        exc,
                    )
                )
                response = ws_module.Response(code=HTTPStatus.INTERNAL_SERVER_ERROR)

            frame.payload = ws_module.JSON.marshal(response).encode(ws_module.UTF_8)
            write_message = cast(
                Callable[[bytes], Any],
                _sdk_attr(cast(Any, self), "_write_message"),
            )
            await write_message(frame.SerializeToString())

    CardCompatibleWSClient.__name__ = "CardCompatibleWSClient"
    CardCompatibleWSClient.__qualname__ = "CardCompatibleWSClient"
    return CardCompatibleWSClient
