"""protocol.py — check events against protocol/v1/event.schema.json.

A small validator for the subset of JSON Schema the protocol files use (type,
required, properties, additionalProperties: false, enum, minimum, maximum,
maxLength, items) — standard library only, so the engine needs no new package.
`dataSchemas` in the event schema holds one schema per event type for `data`.
"""

import json
import pathlib
from functools import lru_cache

from engine import PROTOCOL_VERSION

PROTOCOL_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "protocol" / f"v{PROTOCOL_VERSION}"

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


@lru_cache(maxsize=None)
def load_schema(name: str = "event") -> dict:
    with open(PROTOCOL_DIR / f"{name}.schema.json", encoding="utf-8") as f:
        return json.load(f)


def _is_type(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def check(value, schema: dict, where: str = "") -> "list[str]":
    """Return a list of problems (empty when `value` matches `schema`)."""
    problems: "list[str]" = []
    at = where or "Ereignis"
    want = schema.get("type")
    if want is not None:
        names = want if isinstance(want, list) else [want]
        if not any(_is_type(value, n) for n in names):
            return [f"{at}: erwartet {'/'.join(names)}, gefunden {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{at}: Wert {value!r} nicht erlaubt")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{at}: {value} kleiner als {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{at}: {value} größer als {schema['maximum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        problems.append(f"{at}: Text länger als {schema['maxLength']} Zeichen")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{at}: Pflichtfeld '{key}' fehlt")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    problems.append(f"{at}: unbekanntes Feld '{key}'")
        for key, sub in props.items():
            if key in value:
                problems.extend(check(value[key], sub, f"{at}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            problems.extend(check(item, schema["items"], f"{at}[{i}]"))
    return problems


def check_event(event) -> "list[str]":
    """Check one event: the envelope, then `data` against the schema for its type."""
    schema = load_schema("event")
    problems = check(event, {k: v for k, v in schema.items() if k != "dataSchemas"})
    if problems:
        return problems
    data_schema = schema["dataSchemas"].get(event["type"])
    if data_schema is None:
        return [f"Ereignis: kein Datenschema für Typ '{event['type']}'"]
    return check(event["data"], data_schema, "data")
