# pyright: reportMissingImports=false
"""Shared Local/S3-compatible contracts for Phase 3E attempt objects."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.artifact_storage import (
    InMemoryS3CompatibleAttemptStore,
    LocalAttemptArtifactStore,
    attempt_key,
    parse_attempt_key,
)
from nano_aural_runtime.durable.domain import ArtifactKind
from nano_aural_runtime.durable.errors import StateTransitionError


def _key(kind: ArtifactKind = ArtifactKind.OUTPUT) -> str:
    return attempt_key(str(uuid4()), str(uuid4()), 3, kind, str(uuid4()))


@pytest.fixture(params=("local", "s3"))
def store(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    if request.param == "local":
        value = LocalAttemptArtifactStore(tmp_path / "objects")
        try:
            yield value
        finally:
            value.close()
    else:
        yield InMemoryS3CompatibleAttemptStore()


def test_shared_store_contract_is_immutable_and_streamed(store: object) -> None:
    key = _key()
    first = store.put_stream(key, (b"verified ", b"candidate"))  # type: ignore[attr-defined]
    again = store.put_stream(key, (b"verified candidate",))  # type: ignore[attr-defined]
    assert first.sha256 == again.sha256
    assert first.size_bytes == len(b"verified candidate")
    with store.open_reader(key) as source:  # type: ignore[attr-defined]
        assert source.read() == b"verified candidate"
    with pytest.raises(StateTransitionError):
        store.put_stream(key, (b"different",))  # type: ignore[attr-defined]
    store.delete(key)  # type: ignore[attr-defined]
    store.delete(key)  # type: ignore[attr-defined]
    with pytest.raises((FileNotFoundError, KeyError)):
        store.open_reader(key)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.upper(),
        lambda value: value.replace("attempts/", "/attempts/"),
        lambda value: value.replace("/epoch-3/", "/epoch-03/"),
        lambda value: value.replace("/output/", "/../"),
        lambda value: value + "/extra",
        lambda value: value.replace("/", "\\", 1),
        lambda value: value + "\x00",
    ),
)
def test_parser_rejects_every_noncanonical_spelling(mutate: object) -> None:
    key = _key()
    with pytest.raises(ValueError):
        parse_attempt_key(mutate(key))  # type: ignore[operator]


def test_key_round_trip_binds_attempt_identity() -> None:
    job_id, attempt_id, publication_id = (str(uuid4()) for _ in range(3))
    key = attempt_key(job_id, attempt_id, 9, ArtifactKind.MANIFEST, publication_id)
    assert parse_attempt_key(key) == (
        job_id,
        attempt_id,
        9,
        ArtifactKind.MANIFEST,
        publication_id,
    )


def test_local_store_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    os.symlink(outside, root / "attempts")
    store = LocalAttemptArtifactStore(root)
    try:
        with pytest.raises(OSError):
            store.put_stream(_key(), (b"must-not-escape",))
        assert tuple(outside.iterdir()) == ()
    finally:
        store.close()


def test_local_store_rejects_final_symlink_without_touching_target(tmp_path: Path) -> None:
    root, outside = tmp_path / "objects", tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    store = LocalAttemptArtifactStore(root)
    key = _key()
    parent = root.joinpath(*key.split("/")[:-1])
    parent.mkdir(parents=True)
    os.symlink(outside, parent / key.split("/")[-1])
    try:
        for operation in (
            lambda: store.put_stream(key, (b"replacement",)),
            lambda: store.open_reader(key),
            lambda: store.stat(key),
            lambda: store.delete(key),
        ):
            with pytest.raises((OSError, RuntimeError)):
                operation()
        assert outside.read_bytes() == b"outside"
    finally:
        store.close()


def test_local_concurrent_conditional_put_has_one_immutable_value(tmp_path: Path) -> None:
    store = LocalAttemptArtifactStore(tmp_path / "objects")
    key = _key()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(lambda _index: store.put_stream(key, (b"same", b" bytes")), range(2))
            )
        assert results[0].sha256 == results[1].sha256
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(store.put_stream, key, (b"same bytes",)),
                executor.submit(store.put_stream, key, (b"different",)),
            )
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except StateTransitionError as error:
                    outcomes.append(error)
        assert sum(isinstance(item, StateTransitionError) for item in outcomes) == 1
        with store.open_reader(key) as source:
            assert source.read() == b"same bytes"
    finally:
        store.close()


def test_listing_only_returns_canonical_attempt_objects(store: object) -> None:
    key = _key()
    stored = store.put_stream(key, (b"candidate",))  # type: ignore[attr-defined]
    assert store.list_before(stored.created_at - timedelta(seconds=1)) == ()  # type: ignore[attr-defined]
    assert tuple(
        item.storage_key
        for item in store.list_before(datetime.now(timezone.utc) + timedelta(seconds=1))  # type: ignore[attr-defined]
    ) == (key,)


def test_local_inventory_rechecks_age_after_safe_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "objects"
    store = LocalAttemptArtifactStore(root)
    key = _key()
    try:
        store.put_stream(key, (b"candidate",))
        path = root.joinpath(*key.split("/"))
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        os.utime(path, (old.timestamp(), old.timestamp()))
        cutoff = datetime.now(timezone.utc)
        original_stat = store.stat

        def refresh_then_stat(storage_key: str):
            now = datetime.now(timezone.utc) + timedelta(seconds=1)
            os.utime(path, (now.timestamp(), now.timestamp()))
            return original_stat(storage_key)

        monkeypatch.setattr(store, "stat", refresh_then_stat)
        assert store.list_before(cutoff) == ()
    finally:
        store.close()


def test_local_inventory_page_bounds_hashes_and_persists_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "objects"
    store = LocalAttemptArtifactStore(root)
    keys = tuple(_key() for _ in range(12))
    for key in keys:
        store.put_stream(key, (key.encode("ascii"),))
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    hashed = []
    original_stat = store.stat

    def counted_stat(storage_key: str):
        hashed.append(storage_key)
        return original_stat(storage_key)

    monkeypatch.setattr(store, "stat", counted_stat)
    first = store.inventory_page_before(cutoff, 3, "orphan_sweep")
    assert first.scanned == 3
    assert len(first.objects) == 3
    assert len(hashed) == 3
    first_keys = {item.storage_key for item in first.objects}
    store.close()

    # Cursor evidence lives with the attempt volume, not the process.
    reopened = LocalAttemptArtifactStore(root)
    try:
        second = reopened.inventory_page_before(cutoff, 3, "orphan_sweep")
        assert second.scanned == 3
        assert len(second.objects) == 3
        assert first_keys.isdisjoint(item.storage_key for item in second.objects)
    finally:
        reopened.close()


def test_s3_double_inventory_page_has_the_same_bounded_cursor_contract() -> None:
    store = InMemoryS3CompatibleAttemptStore()
    keys = tuple(_key() for _ in range(8))
    for key in keys:
        store.put_stream(key, (key.encode("ascii"),))
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    first = store.inventory_page_before(cutoff, 3, "orphan_sweep")
    second = store.inventory_page_before(cutoff, 3, "orphan_sweep")
    assert first.scanned == second.scanned == 3
    assert len(first.objects) == len(second.objects) == 3
    assert {item.storage_key for item in first.objects}.isdisjoint(
        item.storage_key for item in second.objects
    )


def test_s3_etag_is_diagnostic_not_content_identity() -> None:
    same_etag = InMemoryS3CompatibleAttemptStore(lambda _key, _data: "same-etag")
    first = same_etag.put_stream(_key(), (b"first",))
    second = same_etag.put_stream(_key(), (b"second",))
    assert first.transport_etag == second.transport_etag
    assert first.sha256 != second.sha256

    etag_by_key = InMemoryS3CompatibleAttemptStore(lambda key, _data: key)
    data = b"same bytes"
    left = etag_by_key.put_stream(_key(), (data,))
    right = etag_by_key.put_stream(_key(), (data,))
    assert left.transport_etag != right.transport_etag
    assert left.sha256 == right.sha256
