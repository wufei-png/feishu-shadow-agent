from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel


def agent_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    _require_all_object_properties(schema)
    return schema


def _require_all_object_properties(value: object) -> None:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        properties = mapping.get("properties")
        if isinstance(properties, dict):
            property_map = cast(dict[str, object], properties)
            mapping["required"] = list(property_map)
            mapping.setdefault("additionalProperties", False)
        for child in mapping.values():
            _require_all_object_properties(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            _require_all_object_properties(child)
