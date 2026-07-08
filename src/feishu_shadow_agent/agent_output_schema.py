from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def agent_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    _require_all_object_properties(schema)
    return schema


def _require_all_object_properties(value: Any) -> None:
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties.keys())
            value.setdefault("additionalProperties", False)
        for child in value.values():
            _require_all_object_properties(child)
    elif isinstance(value, list):
        for child in value:
            _require_all_object_properties(child)
