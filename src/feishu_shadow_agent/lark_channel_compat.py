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
from http import HTTPStatus
from typing import Any

_SDK_PATCH_LOCK = threading.RLock()


def create_channel(app_id: str, app_secret: str) -> CompatibleFeishuChannel:
    return CompatibleFeishuChannel(app_id=app_id, app_secret=app_secret)


class CompatibleFeishuChannel:
    """Small delegating facade that isolates SDK lifecycle quirks."""

    def __init__(self, *, app_id: str, app_secret: str) -> None:
        ws_module = importlib.import_module("lark_channel.ws.client")
        channel_module = importlib.import_module("lark_channel.channel.channel")
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

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None:
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


def _build_card_compatible_ws_client(base_cls: type, ws_module: Any) -> type:
    """Create a version-local WS client with the missing CARD dispatch branch."""

    class CardCompatibleWSClient(base_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # The SDK constructs this object in an executor thread. Setting the
            # loop there prevents asyncio primitives from binding to a missing
            # executor-thread loop or to the caller's running loop.
            asyncio.set_event_loop(ws_module.loop)
            super().__init__(*args, **kwargs)

        async def _handle_data_frame(self, frame: Any) -> None:
            # This mirrors lark-channel-sdk's implementation, with CARD treated
            # like EVENT so the registered dispatcher can parse the payload and
            # the platform receives the normal acknowledgement frame.
            hs = frame.headers
            msg_id = ws_module._get_by_key(hs, ws_module.HEADER_MESSAGE_ID)
            trace_id = ws_module._get_by_key(hs, ws_module.HEADER_TRACE_ID)
            sum_ = ws_module._get_by_key(hs, ws_module.HEADER_SUM)
            seq = ws_module._get_by_key(hs, ws_module.HEADER_SEQ)
            type_ = ws_module._get_by_key(hs, ws_module.HEADER_TYPE)

            payload = frame.payload
            if int(sum_) > 1:
                payload = self._combine(msg_id, int(sum_), int(seq), payload)
                if payload is None:
                    return

            message_type = ws_module.MessageType(type_)
            ws_module.logger.debug(
                self._fmt_log(
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
                start = int(round(ws_module.time.time() * 1000))
                result = self._event_handler._do_without_validation(payload)
                end = int(round(ws_module.time.time() * 1000))
                header = hs.add()
                header.key = ws_module.HEADER_BIZ_RT
                header.value = str(end - start)
                if result is not None:
                    response.data = ws_module.base64.b64encode(
                        ws_module.JSON.marshal(result).encode(ws_module.UTF_8)
                    )
            except Exception as exc:
                ws_module.logger.error(
                    self._fmt_log(
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
            await self._write_message(frame.SerializeToString())

    CardCompatibleWSClient.__name__ = "CardCompatibleWSClient"
    CardCompatibleWSClient.__qualname__ = "CardCompatibleWSClient"
    return CardCompatibleWSClient
