"""Experimental, default-off Woosh V2A feature caches. Result-preserving only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping, Optional

from nano_aural_runtime import CacheReport

ALLOWED_CACHE_KINDS = frozenset(
    (
        "video_preprocess",
        "synchformer_features",
        "text_tokens",
        "unconditional_condition",
    )
)
FORBIDDEN_CACHE_KINDS = frozenset(
    (
        "ode_trajectory",
        "latent",
        "step_skip",
        "cross_seed",
    )
)
WOOSH_CACHE_MODE_OFF = "off"
WOOSH_CACHE_MODE_FEATURES = "experimental_features"


class WooshCacheError(ValueError):
    """A cache kind or key is not allowed for V1 Woosh V2A."""


def cache_key(kind: str, payload: Mapping[str, object]) -> str:
    if kind in FORBIDDEN_CACHE_KINDS:
        raise WooshCacheError("forbidden Woosh cache kind")
    if kind not in ALLOWED_CACHE_KINDS:
        raise WooshCacheError("unknown Woosh cache kind")
    if "seed" in payload:
        raise WooshCacheError("feature caches must not be keyed by seed")
    encoded = json.dumps(
        {"kind": kind, **{str(key): payload[key] for key in sorted(payload)}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class WooshFeatureCache:
    mode: str = WOOSH_CACHE_MODE_OFF
    _store: dict[str, bytes] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        if self.mode not in (WOOSH_CACHE_MODE_OFF, WOOSH_CACHE_MODE_FEATURES):
            raise WooshCacheError("cache mode must be off or experimental_features")

    @property
    def enabled(self) -> bool:
        return self.mode == WOOSH_CACHE_MODE_FEATURES

    def get(self, kind: str, payload: Mapping[str, object]) -> Optional[bytes]:
        key = cache_key(kind, payload)
        if not self.enabled:
            self.misses += 1
            return None
        value = self._store.get(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, kind: str, payload: Mapping[str, object], value: bytes) -> None:
        if not isinstance(value, bytes) or not value:
            raise WooshCacheError("cache values must be non-empty bytes")
        key = cache_key(kind, payload)
        if not self.enabled:
            return
        self._store[key] = value

    def report(self) -> CacheReport:
        return CacheReport(
            hits=self.hits,
            misses=self.misses,
            bytes_used=sum(len(item) for item in self._store.values()),
            metadata={"mode": self.mode, "enabled": self.enabled},
        )
