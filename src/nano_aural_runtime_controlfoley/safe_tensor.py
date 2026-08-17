"""Minimal strict safetensors validation for adapter-owned cache payloads.

The parser deliberately accepts data-only safetensors.  It never imports
torch, pickle, or an operator-selected decoder.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

_FORMAT = "nano-aural-controlfoley-condition"
_MAX_HEADER_BYTES = 64 * 1024
_DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "F64": 8,
    "I64": 8,
    "U64": 8,
}


@dataclass(frozen=True)
class SafeTensorDescriptor:
    name: str
    dtype: str
    shape: Tuple[int, ...]
    data_start: int
    data_end: int


@dataclass(frozen=True)
class SafeTensorBundle:
    codec_version: str
    schema_version: str
    tensors: Tuple[SafeTensorDescriptor, ...]
    data: bytes

    def tensor_bytes(self, name: str) -> bytes:
        for tensor in self.tensors:
            if tensor.name == name:
                return self.data[tensor.data_start : tensor.data_end]
        raise ValueError("safe tensor bundle does not contain the requested tensor")


def build_u8_safe_tensor_bundle(
    value: bytes,
    *,
    codec_version: str,
    schema_version: str,
    tensor_name: str = "condition",
) -> bytes:
    """Build a deterministic data-only safetensors bundle for CPU tests/codecs."""

    if not isinstance(value, bytes) or not value:
        raise ValueError("safe tensor value must be non-empty immutable bytes")
    _safe_identifier(codec_version, "codec_version")
    _safe_identifier(schema_version, "schema_version")
    _tensor_name(tensor_name)
    header = {
        "__metadata__": {
            "format": _FORMAT,
            "codec_version": codec_version,
            "schema_version": schema_version,
        },
        tensor_name: {
            "dtype": "U8",
            "shape": [len(value)],
            "data_offsets": [0, len(value)],
        },
    }
    header_bytes = json.dumps(
        header, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + value


def validate_safe_tensor_bundle(
    payload: bytes,
    *,
    codec_version: str,
    schema_version: str,
    max_size_bytes: Optional[int] = None,
) -> SafeTensorBundle:
    """Strictly validate a bounded safetensors payload and return byte views."""

    _safe_identifier(codec_version, "codec_version")
    _safe_identifier(schema_version, "schema_version")
    if not isinstance(payload, bytes) or len(payload) <= 8:
        raise ValueError("safe tensor payload is truncated")
    if max_size_bytes is not None:
        if (
            isinstance(max_size_bytes, bool)
            or not isinstance(max_size_bytes, int)
            or max_size_bytes <= 0
        ):
            raise ValueError("max_size_bytes must be a positive integer")
        if len(payload) > max_size_bytes:
            raise ValueError("safe tensor payload exceeds its sealed byte limit")
    header_size = struct.unpack("<Q", payload[:8])[0]
    if header_size == 0 or header_size > _MAX_HEADER_BYTES or 8 + header_size >= len(payload):
        raise ValueError("safe tensor header length is invalid")
    header_bytes = payload[8 : 8 + header_size]
    if not header_bytes.startswith(b"{"):
        raise ValueError("safe tensor header must begin with an object")
    try:
        header = json.loads(header_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("safe tensor header is invalid") from error
    if not isinstance(header, dict) or not header:
        raise ValueError("safe tensor header must be a non-empty object")
    metadata = header.pop("__metadata__", None)
    expected_metadata = {
        "format": _FORMAT,
        "codec_version": codec_version,
        "schema_version": schema_version,
    }
    if metadata != expected_metadata or not header:
        raise ValueError("safe tensor metadata does not match the sealed codec")
    data = payload[8 + header_size :]
    tensors = []
    ranges = []
    for name, value in header.items():
        _tensor_name(name)
        if not isinstance(value, dict) or set(value) != {"dtype", "shape", "data_offsets"}:
            raise ValueError("safe tensor descriptor has unexpected fields")
        dtype = value["dtype"]
        if not isinstance(dtype, str) or dtype not in _DTYPE_BYTES:
            raise ValueError("safe tensor dtype is unsupported")
        shape = value["shape"]
        offsets = value["data_offsets"]
        if (
            not isinstance(shape, list)
            or not shape
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in shape
            )
            or not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in offsets)
        ):
            raise ValueError("safe tensor shape or offsets are invalid")
        start, end = offsets
        expected_bytes = _DTYPE_BYTES[dtype]
        for dimension in shape:
            expected_bytes *= dimension
            if expected_bytes > len(data):
                raise ValueError("safe tensor shape exceeds the payload")
        if start < 0 or end <= start or end > len(data) or end - start != expected_bytes:
            raise ValueError("safe tensor byte range is invalid")
        ranges.append((start, end))
        tensors.append(SafeTensorDescriptor(name, dtype, tuple(shape), start, end))
    cursor = 0
    for start, end in sorted(ranges):
        if start != cursor:
            raise ValueError("safe tensor ranges must be non-overlapping and cover all data")
        cursor = end
    if cursor != len(data):
        raise ValueError("safe tensor ranges must be non-overlapping and cover all data")
    return SafeTensorBundle(codec_version, schema_version, tuple(tensors), data)


def safe_tensor_metadata(bundle: SafeTensorBundle) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "codec_version": bundle.codec_version,
            "schema_version": bundle.schema_version,
            "tensor_count": len(bundle.tensors),
            "data_bytes": len(bundle.data),
        }
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("safe tensor JSON contains duplicate keys")
        result[key] = value
    return result


def _tensor_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise ValueError("safe tensor name must be a short ASCII identifier")
    return value


def _safe_identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise ValueError("{0} must be a short ASCII identifier".format(name))
    return value
