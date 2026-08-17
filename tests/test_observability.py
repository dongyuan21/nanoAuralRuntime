# pyright: reportMissingImports=false
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from nano_aural_runtime.durable.observability import (
    DurableHttpObserver,
    DurableMetrics,
    StructuredEventLogger,
)


def test_metrics_accept_only_fixed_low_cardinality_labels() -> None:
    metrics = DurableMetrics()
    labels = {"stage": "finalize", "outcome": "success"}
    metrics.increment("nano_aural_publications_total", labels)
    metrics.increment("nano_aural_publications_total", labels, 2)
    sample = metrics.snapshot()[0]
    assert sample.name == "nano_aural_publications_total"
    assert sample.labels == labels
    assert sample.value == 3

    with pytest.raises(ValueError, match="schema"):
        metrics.increment(
            "nano_aural_publications_total",
            {**labels, "job_id": "private-job"},
        )
    with pytest.raises(ValueError, match="fixed domain"):
        metrics.increment(
            "nano_aural_publications_total",
            {"stage": "attempt/private-path", "outcome": "success"},
        )


def test_structured_logger_cannot_accept_identifiers_paths_or_tokens() -> None:
    output = io.StringIO()
    logger = StructuredEventLogger(
        output,
        clock=lambda: datetime(2026, 8, 17, 1, 2, 3, tzinfo=timezone.utc),
    )
    logger.emit(
        component="publication",
        outcome="rejected",
        reason_code="media_validation",
        numeric={"bytes": 4096, "duration_ms": 2.5},
    )
    value = json.loads(output.getvalue())
    assert value == {
        "bytes": 4096,
        "component": "publication",
        "duration_ms": 2.5,
        "outcome": "rejected",
        "reason_code": "media_validation",
        "timestamp": "2026-08-17T01:02:03Z",
    }

    for prohibited in ("job_id", "path", "prompt", "token"):
        with pytest.raises(ValueError, match="numeric fields"):
            logger.emit(component="api", outcome="failed", numeric={prohibited: 1})
    with pytest.raises(ValueError, match="reason code"):
        logger.emit(component="api", outcome="failed", reason_code="Bearer secret")


def test_observability_rejects_unbounded_or_invalid_numbers() -> None:
    metrics = DurableMetrics()
    with pytest.raises(ValueError, match="positive"):
        metrics.increment("nano_aural_download_integrity_total", {"outcome": "success"}, 0)
    logger = StructuredEventLogger(io.StringIO())
    with pytest.raises(ValueError, match="finite"):
        logger.emit(component="worker", outcome="failed", numeric={"duration_ms": float("inf")})


def test_http_observer_reduces_identifiers_to_fixed_routes() -> None:
    output = io.StringIO()
    metrics = DurableMetrics()
    observer = DurableHttpObserver(
        metrics,
        StructuredEventLogger(
            output,
            clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
        ),
    )
    observer.record("GET", "/v1/jobs/private-job/artifacts/private-artifact/content", 200, 1.5)
    sample = metrics.snapshot()[0]
    assert sample.labels == {"method": "GET", "outcome": "success", "route": "download"}
    assert "private-job" not in output.getvalue()
    assert "private-artifact" not in output.getvalue()

    observer.record("DELETE", "/private/operator/path", 404, 1)
    unknown = next(sample for sample in metrics.snapshot() if sample.labels["route"] == "unknown")
    assert unknown.labels == {"method": "OTHER", "outcome": "client_error", "route": "unknown"}
