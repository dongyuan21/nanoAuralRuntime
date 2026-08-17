"""Bounded, low-cardinality observability for the durable service.

This module deliberately accepts no job, attempt, artifact, prompt, path, URL,
or credential fields.  Operators can correlate detailed state through the
authorized database/API while metrics and service logs remain safe to export.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from types import MappingProxyType
from typing import Callable, Mapping, Optional, TextIO, Tuple

_METRIC_LABELS = {
    "nano_aural_api_requests_total": {
        "route": frozenset(
            ("jobs", "job", "cancel", "events", "artifacts", "download", "upload", "unknown")
        ),
        "method": frozenset(("GET", "POST", "PUT", "OTHER")),
        "outcome": frozenset(("success", "client_error", "server_error")),
    },
    "nano_aural_publications_total": {
        "stage": frozenset(("reserve", "object", "validate", "canonical", "finalize", "cleanup")),
        "outcome": frozenset(("success", "rejected", "conflict", "lease_lost", "failed")),
    },
    "nano_aural_lease_events_total": {
        "event": frozenset(("heartbeat", "cancel", "lost", "reaped", "retry")),
        "outcome": frozenset(("success", "conflict", "failed")),
    },
    "nano_aural_orphan_actions_total": {
        "action": frozenset(("retain", "abandon", "delete")),
        "outcome": frozenset(("success", "active", "grace", "failed")),
    },
    "nano_aural_download_integrity_total": {
        "outcome": frozenset(("success", "checksum_mismatch", "size_mismatch", "limit", "failed")),
    },
}

_EVENT_COMPONENTS = frozenset(("api", "queue", "worker", "publication", "orphan_sweeper"))
_EVENT_OUTCOMES = frozenset(
    ("started", "success", "rejected", "cancelled", "conflict", "lease_lost", "failed")
)
_REASON_CODES = frozenset(
    (
        "authorization",
        "bad_request",
        "checksum",
        "content_type",
        "database",
        "lease",
        "media_validation",
        "object_store",
        "size_limit",
        "worker_process",
    )
)
_NUMERIC_FIELDS = frozenset(("bytes", "count", "duration_ms"))


@dataclass(frozen=True)
class CounterSample:
    name: str
    labels: Mapping[str, str]
    value: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))


class DurableMetrics:
    """Thread-safe counters whose complete label domain is fixed in code."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[Tuple[str, Tuple[Tuple[str, str], ...]], int] = {}

    def increment(self, name: str, labels: Mapping[str, str], amount: int = 1) -> None:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("metric amount must be a positive integer")
        normalized = self._validate(name, labels)
        key = (name, normalized)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def snapshot(self) -> Tuple[CounterSample, ...]:
        with self._lock:
            values = tuple(sorted(self._values.items()))
        return tuple(CounterSample(name, dict(labels), value) for (name, labels), value in values)

    @staticmethod
    def _validate(name: str, labels: Mapping[str, str]) -> Tuple[Tuple[str, str], ...]:
        schema = _METRIC_LABELS.get(name)
        if schema is None:
            raise ValueError("metric name is not in the durable allowlist")
        if not isinstance(labels, Mapping) or set(labels) != set(schema):
            raise ValueError("metric labels do not match the fixed schema")
        normalized = []
        for key in sorted(schema):
            value = labels[key]
            if not isinstance(value, str) or value not in schema[key]:
                raise ValueError("metric label value is not in the fixed domain")
            normalized.append((key, value))
        return tuple(normalized)


class StructuredEventLogger:
    """Emit one strict JSON object per operational event.

    Only fixed categorical values and bounded numeric observations are
    accepted.  Identifiers and arbitrary strings therefore cannot leak into
    logs through this interface.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._lock = Lock()

    def emit(
        self,
        *,
        component: str,
        outcome: str,
        reason_code: Optional[str] = None,
        numeric: Mapping[str, float] = MappingProxyType({}),
    ) -> None:
        if component not in _EVENT_COMPONENTS:
            raise ValueError("event component is not in the allowlist")
        if outcome not in _EVENT_OUTCOMES:
            raise ValueError("event outcome is not in the allowlist")
        if reason_code is not None and reason_code not in _REASON_CODES:
            raise ValueError("event reason code is not in the allowlist")
        if not isinstance(numeric, Mapping) or not set(numeric).issubset(_NUMERIC_FIELDS):
            raise ValueError("event numeric fields are not in the allowlist")
        payload: dict[str, object] = {
            "component": component,
            "outcome": outcome,
            "timestamp": self._timestamp(),
        }
        if reason_code is not None:
            payload["reason_code"] = reason_code
        for name, value in numeric.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("event numeric values must be numbers")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("event numeric values must be finite and non-negative")
            payload[name] = value
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._stream.write(encoded + "\n")
            self._stream.flush()

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("observability clock must return an aware datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DurableHttpObserver:
    """Reduce HTTP observations to fixed route classes before export."""

    def __init__(self, metrics: DurableMetrics, events: StructuredEventLogger) -> None:
        self._metrics = metrics
        self._events = events

    def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
        route = self._route(path)
        observed_method = method if method in ("GET", "POST", "PUT") else "OTHER"
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("HTTP status is invalid")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
            raise TypeError("HTTP duration must be numeric")
        duration = float(duration_ms)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("HTTP duration must be finite and non-negative")
        if status < 400:
            metric_outcome, event_outcome = "success", "success"
        elif status < 500:
            metric_outcome, event_outcome = "client_error", "rejected"
        else:
            metric_outcome, event_outcome = "server_error", "failed"
        self._metrics.increment(
            "nano_aural_api_requests_total",
            {"route": route, "method": observed_method, "outcome": metric_outcome},
        )
        self._events.emit(
            component="api",
            outcome=event_outcome,
            numeric={"duration_ms": duration},
        )

    @staticmethod
    def _route(path: str) -> str:
        if not isinstance(path, str):
            raise TypeError("HTTP path must be a string")
        clean = path.split("?", 1)[0]
        segments = tuple(item for item in clean.split("/") if item)
        if segments[:2] != ("v1", "jobs") and segments[:3] != (
            "v1",
            "assets",
            "uploads",
        ):
            return "unknown"
        if segments[:3] == ("v1", "assets", "uploads"):
            return "upload"
        if len(segments) == 2:
            return "jobs"
        if len(segments) == 3:
            return "job"
        suffix = segments[3]
        if suffix == "cancel":
            return "cancel"
        if suffix == "events":
            return "events"
        if suffix == "artifacts":
            return "artifacts" if len(segments) == 4 else "download"
        return "unknown"
