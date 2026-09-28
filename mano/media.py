"""Screenshot inspection and VLM image-encoding helpers."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from .errors import ManoError


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        header = fh.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ManoError(f"not a valid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def image_data_url(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 7_000_000:
        raise ManoError(f"screenshot exceeds the safe Base64 limit (7 MB): {len(raw)} bytes")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
