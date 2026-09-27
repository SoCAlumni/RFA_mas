"""Minimal OpenAPI 3.1 schema checker for teammate contract fixtures (no jsonschema dep).

Supports the subset used by RFA_module's contracts: type, required, properties, items,
enum, const, minimum/maximum, pattern, format=date/date-time/uri and local/cross-file $ref.
Unknown keywords are ignored; failures list the JSON path.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


class OpenApiDocument:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.document = yaml.safe_load(path.read_text(encoding="utf-8"))
        self._siblings: dict[str, OpenApiDocument] = {}

    def schema(self, name: str) -> dict[str, Any]:
        return self.document["components"]["schemas"][name]

    def response_schema(self, path: str, method: str, status: str) -> dict[str, Any]:
        response = self.document["paths"][path][method]["responses"][status]
        return response["content"]["application/json"]["schema"]

    def resolve(self, ref: str) -> tuple[OpenApiDocument, dict[str, Any]]:
        file_part, _, pointer = ref.partition("#")
        document = self
        if file_part:
            sibling = self._siblings.get(file_part)
            if sibling is None:
                sibling = OpenApiDocument(self.path.parent / file_part)
                self._siblings[file_part] = sibling
            document = sibling
        node: Any = document.document
        for part in pointer.strip("/").split("/"):
            node = node[part]
        return document, node

    def validate(self, schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
        if "$ref" in schema:
            document, target = self.resolve(schema["$ref"])
            return document.validate(target, value, path)
        errors: list[str] = []
        expected = schema.get("type")
        if expected is not None and not _is_type(expected, value):
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: {value!r} not in enum")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: below minimum")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{path}: above maximum")
        if isinstance(value, str):
            if "pattern" in schema and re.search(schema["pattern"], value) is None:
                errors.append(f"{path}: pattern mismatch")
            errors.extend(_format_errors(schema.get("format"), value, path))
        if isinstance(value, dict):
            for key in schema.get("required", ()):
                if key not in value:
                    errors.append(f"{path}.{key}: required")
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    errors.extend(self.validate(sub, value[key], f"{path}.{key}"))
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append(f"{path}: fewer than minItems")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append(f"{path}: more than maxItems")
            items = schema.get("items")
            if items is not None:
                for index, item in enumerate(value):
                    errors.extend(self.validate(items, item, f"{path}[{index}]"))
        return errors


def _is_type(expected: str | list[str], value: Any) -> bool:
    if isinstance(expected, list):
        return any(_is_type(item, value) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _format_errors(fmt: str | None, value: str, path: str) -> list[str]:
    try:
        if fmt == "date":
            date.fromisoformat(value)
        elif fmt == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif fmt == "uri" and "://" not in value:
            return [f"{path}: not a uri"]
    except ValueError:
        return [f"{path}: invalid {fmt}"]
    return []
