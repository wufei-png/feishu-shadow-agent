from __future__ import annotations

from typing import Any

from .types import FeedbackReason

CARD_ACTION_PROTOCOL = "feishu_shadow_agent.approval.v1"
FEEDBACK_REASON_OPTIONS: tuple[tuple[FeedbackReason, str], ...] = (
    ("inaccurate_or_unsupported", "不准确或缺少依据"),
    ("incomplete_context", "上下文不完整"),
    ("tone_or_style", "语气或风格"),
    ("unnecessary_reply", "无需回复"),
    ("other", "其他"),
)


def build_approval_card(payload: dict[str, Any]) -> dict[str, Any]:
    approval_id = _required_string(payload, "approval_id")
    task_id = _string(payload.get("task_id")) or "unknown"
    reason = _string(payload.get("reason")) or "human review required"
    incoming = _incoming_text(payload.get("incoming_message"))
    suggested = _string(payload.get("suggested_reply"))
    source = _source_text(payload.get("source"))
    approvable = payload.get("approvable") is not False and bool(suggested.strip())

    details = [
        f"任务：{task_id}",
        f"审批：{approval_id}",
        f"原因：{reason}",
    ]
    if source:
        details.append(f"来源：{source}")
    if incoming:
        details.append(f"原消息：{incoming}")
    details.append(f"建议回复：{suggested or '<无>'}")

    buttons: list[dict[str, Any]] = []
    if approvable:
        buttons.append(
            _button("发送建议", "send_suggestion", approval_id, style="primary")
        )
    buttons.extend(
        [
            _button("编辑后发送", "edit_send", approval_id),
            _button("不发送，继续观察", "no_send_keep_watching", approval_id),
            _button(
                "不发送，结束任务",
                "no_send_end_task",
                approval_id,
                style="danger",
            ),
        ]
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "回复需要你的确认"},
            "template": "orange",
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": _bounded("\n".join(details), 3000),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "form",
                    "name": "approval_form",
                    "elements": [
                        {
                            "tag": "input",
                            "element_id": "final_reply",
                            "name": "final_reply",
                            "label": {
                                "tag": "plain_text",
                                "content": "编辑后回复",
                            },
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "仅“编辑后发送”要求非空",
                            },
                            "default_value": _bounded(suggested, 1000),
                            "input_type": "multiline_text",
                            "rows": 4,
                            "max_length": 1000,
                            "required": False,
                        },
                        {
                            "tag": "select_static",
                            "element_id": "feedback_reason",
                            "name": "feedback_reason",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "反馈原因（可选）",
                            },
                            "options": [
                                {
                                    "text": {
                                        "tag": "plain_text",
                                        "content": label,
                                    },
                                    "value": value,
                                }
                                for value, label in FEEDBACK_REASON_OPTIONS
                            ],
                        },
                        {
                            "tag": "input",
                            "element_id": "feedback_note",
                            "name": "note",
                            "label": {"tag": "plain_text", "content": "备注（可选）"},
                            "max_length": 500,
                            "required": False,
                        },
                        {
                            "tag": "column_set",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [button],
                                }
                                for button in buttons
                            ],
                        },
                    ],
                },
            ]
        },
    }


def _button(
    label: str, action: str, approval_id: str, *, style: str = "default"
) -> dict[str, Any]:
    button_names = {
        "send_suggestion": "btn_send",
        "edit_send": "btn_edit",
        "no_send_keep_watching": "btn_keep",
        "no_send_end_task": "btn_end",
    }
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": style,
        "action_type": "form_submit",
        "name": button_names[action],
        "value": {
            "protocol": CARD_ACTION_PROTOCOL,
            "action": action,
            "approval_id": approval_id,
        },
    }


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = _string(payload.get(key))
    if not value:
        raise ValueError(f"approval card payload is missing {key}")
    if key == "approval_id" and not value.startswith("a_"):
        raise ValueError("approval card requires a concrete approval_id")
    return value


def _incoming_text(value: Any) -> str:
    if isinstance(value, dict):
        return _bounded(_string(value.get("text")), 800)
    return _bounded(_string(value), 800)


def _source_text(value: Any) -> str:
    if not isinstance(value, dict):
        return _bounded(_string(value), 300)
    return " / ".join(
        part
        for part in (
            _string(value.get("chat_type")),
            _string(value.get("chat_id")),
            _string(value.get("sender_name") or value.get("sender_id")),
        )
        if part
    )


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."
