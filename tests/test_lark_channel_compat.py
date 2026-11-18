from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

pytest.importorskip("lark_channel")

from lark_channel.channel.bot_identity import BotIdentity
from lark_channel.ws import client as ws_module
from lark_channel.ws.pb.pbbp2_pb2 import Frame

from feishu_shadow_agent.lark_channel_compat import create_channel


def test_raw_message_is_persisted_before_sdk_dispatch_returns() -> None:
    async def run() -> None:
        channel = create_channel("cli_test", "secret_test")
        inner = cast(Any, channel._channel)
        inner._bot_identity = BotIdentity(open_id="ou_bot")
        observed: list[Any] = []
        inner._on_p2_im_message_receive_v1 = lambda _data: observed.append("scheduled")
        channel.set_raw_message_handler(lambda raw, bot: observed.append((raw, bot)))
        dispatcher = inner._build_dispatcher()
        payload = {
            "schema": "2.0",
            "header": {
                "event_type": "im.message.receive_v1",
                "event_id": "evt_1",
                "app_id": "cli_test",
                "tenant_key": "tenant",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"hello"}',
                    "mentions": [{"id": {"open_id": "ou_bot"}, "key": "@_user_1"}],
                },
            },
        }
        dispatcher._do_without_validation(json.dumps(payload).encode())
        raw, bot = observed[0]
        assert bot == "ou_bot"
        assert raw["event"]["message"]["message_id"] == "om_1"
        assert raw["event"]["message"]["mentions"][0]["id"]["open_id"] == "ou_bot"
        assert observed[1] == "scheduled"
        await channel.disconnect()

    asyncio.run(run())


def test_raw_message_failure_propagates_before_acknowledgement() -> None:
    async def run() -> None:
        channel = create_channel("cli_test", "secret_test")
        inner = cast(Any, channel._channel)
        inner._bot_identity = BotIdentity(open_id="ou_bot")

        def fail(_raw: object, _bot: str) -> None:
            raise RuntimeError("storage unavailable")

        channel.set_raw_message_handler(fail)
        dispatcher = inner._build_dispatcher()
        payload = {
            "schema": "2.0",
            "header": {
                "event_type": "im.message.receive_v1",
                "event_id": "evt_1",
                "app_id": "cli_test",
            },
            "event": {
                "sender": {},
                "message": {"message_id": "om_1", "chat_id": "oc_1"},
            },
        }
        with pytest.raises(RuntimeError, match="storage unavailable"):
            dispatcher._do_without_validation(json.dumps(payload).encode())
        await channel.disconnect()

    asyncio.run(run())


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
