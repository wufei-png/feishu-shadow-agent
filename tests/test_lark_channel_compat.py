from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("lark_channel")

from lark_channel.ws import client as ws_module
from lark_channel.ws.pb.pbbp2_pb2 import Frame

from feishu_shadow_agent.lark_channel_compat import create_channel


def test_channel_uses_an_idle_sdk_loop_outside_project_loop() -> None:
    previous_loop = ws_module.loop

    async def run() -> None:
        running_loop = asyncio.get_running_loop()
        channel = create_channel("cli_test", "secret_test")
        assert channel.sdk_loop is not running_loop
        assert channel.sdk_loop.is_running() is False
        assert ws_module.loop is channel.sdk_loop
        await channel.disconnect()

    asyncio.run(run())
    assert ws_module.loop is previous_loop


def test_card_data_frame_reaches_event_dispatcher() -> None:
    async def run() -> None:
        channel = create_channel("cli_test", "secret_test")
        client_class = channel._ws_client_class
        client = object.__new__(client_class)
        dispatched: list[bytes] = []
        writes: list[bytes] = []

        class Handler:
            def _do_without_validation(self, payload: bytes) -> dict[str, bool]:
                dispatched.append(payload)
                return {"ok": True}

        client._event_handler = Handler()
        client._combine = lambda _message_id, _total, _seq, payload: payload
        client._fmt_log = lambda fmt, *args: fmt.format(*args)

        async def write(payload: bytes) -> None:
            writes.append(payload)

        client._write_message = write
        frame = Frame()
        frame.method = 1
        frame.SeqID = 1
        frame.LogID = 1
        frame.service = 1
        for key, value in (
            (ws_module.HEADER_MESSAGE_ID, "message-1"),
            (ws_module.HEADER_TRACE_ID, "trace-1"),
            (ws_module.HEADER_SUM, "1"),
            (ws_module.HEADER_SEQ, "0"),
            (ws_module.HEADER_TYPE, ws_module.MessageType.CARD.value),
        ):
            header = frame.headers.add()
            header.key = key
            header.value = value
        frame.payload = b'{"schema":"2.0"}'

        await client._handle_data_frame(frame)
        assert dispatched == [b'{"schema":"2.0"}']
        assert writes
        await channel.disconnect()

    asyncio.run(run())
