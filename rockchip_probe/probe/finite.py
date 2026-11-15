"""Strict JSON for the report: json.dumps writes bare NaN/Infinity, which no
strict parser accepts, so non-finite numbers become null and are listed."""

import json
import math


def sanitize(value, path="$", markers=None):
    if markers is None:
        markers = []
    # numpy scalars and arrays coming back from inference
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        value = value.tolist()
    if isinstance(value, float):
        if math.isfinite(value):
            return value, markers
        markers.append(f"{path}: {value!r}")
        return None, markers
    if isinstance(value, dict):
        return {k: sanitize(v, f"{path}.{k}", markers)[0] for k, v in value.items()}, markers
    if isinstance(value, (list, tuple)):
        return [sanitize(v, f"{path}[{i}]", markers)[0] for i, v in enumerate(value)], markers
    return value, markers


def dumps(value, **kwargs):
    clean, markers = sanitize(value)
    if markers and isinstance(clean, dict):
        clean["non_finite_values"] = markers
    return json.dumps(clean, allow_nan=False, **kwargs)
