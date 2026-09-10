"""Canonical JSON identity with explicit types; never default=str coercion."""

import hashlib
import json
import math


def _tag(value):
    kind = type(value)
    if value is None:
        return ["null"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", str(value)]
    if kind is float and math.isfinite(value):
        return ["float", value.hex()]
    if kind is str:
        value.encode("utf-8")
        return ["str", value]
    if kind is list:
        return ["list", [_tag(item) for item in value]]
    if kind is dict and all(type(key) is str for key in value):
        return ["dict", [[key, _tag(value[key])] for key in sorted(value)]]
    raise ValueError("Unsupported typed JSON value.")


def canonical_bytes(value):
    return json.dumps(_tag(value), ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def json_bytes(value):
    canonical_bytes(value)  # Enforce finite, UTF-8 JSON types before serialization.
    return json.dumps(value, allow_nan=False).encode("utf-8")


def parse_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result

    value = json.loads(body, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON.")))
    canonical_bytes(value)
    return value
