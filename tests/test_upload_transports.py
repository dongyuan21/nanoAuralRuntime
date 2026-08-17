"""Shared Local/S3-compatible transport contracts for Phase 3B."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.storage import LocalBlobStore
from nano_aural_runtime.durable.upload_transports import (
    InMemoryS3CompatibleCanonical,
    MultipartUploadBuffer,
)
from nano_aural_runtime.durable.uploads import InMemoryS3CompatibleStaging, LocalStagingBlobStore


@pytest.mark.parametrize("kind", ("local", "s3"))
def test_canonical_blob_contract_parity(tmp_path: Path, kind: str) -> None:
    store = (
        LocalBlobStore(tmp_path / "local") if kind == "local" else InMemoryS3CompatibleCanonical()
    )
    first = store.put_stream((b"same", b" bytes"))
    second = store.put_stream((b"same bytes",))
    assert first == second
    assert first.sha256 == hashlib.sha256(b"same bytes").hexdigest()
    with store.open_reader(first.storage_key) as reader:
        assert reader.read() == b"same bytes"
    assert store.stat(first.storage_key) == first
    with pytest.raises(ValueError):
        store.open_reader("../not-a-canonical-key")
    if isinstance(store, LocalBlobStore):
        store.close()


@pytest.mark.parametrize("kind", ("local", "s3"))
def test_staging_contract_parity(tmp_path: Path, kind: str) -> None:
    store = (
        LocalStagingBlobStore(tmp_path / "local")
        if kind == "local"
        else InMemoryS3CompatibleStaging()
    )
    session = "00000000-0000-0000-0000-000000000021"
    key = store.write_stream(session, (b"one", b"two"))
    with store.open_reader(key) as reader:
        assert reader.read() == b"onetwo"
    with pytest.raises(StateTransitionError):
        store.write_stream(session, (b"replacement",))
    with pytest.raises(ValueError):
        store.open_reader("staging/../outside")
    store.delete(key)
    assert not store.exists(key)
    if isinstance(store, LocalStagingBlobStore):
        store.close()


def test_s3_etag_is_only_transport_diagnostic() -> None:
    staging = InMemoryS3CompatibleStaging()
    first = staging.write_stream("00000000-0000-0000-0000-000000000022", (b"same",))
    second = staging.write_stream("00000000-0000-0000-0000-000000000023", (b"same",))
    third = staging.write_stream("00000000-0000-0000-0000-000000000024", (b"different",))
    staging._etags[second] = "different-provider-etag"  # type: ignore[attr-defined]
    staging._etags[third] = staging.multipart_etag(first)  # type: ignore[attr-defined]
    canonical = InMemoryS3CompatibleCanonical()
    assert canonical.put_stream((b"same",)).sha256 == canonical.put_stream((b"same",)).sha256
    assert canonical.put_stream((b"same",)).sha256 != canonical.put_stream((b"different",)).sha256


def test_multipart_buffer_requires_contiguous_immutable_parts_and_terminal_state() -> None:
    upload = MultipartUploadBuffer()
    upload.put_part(2, b"two")
    with pytest.raises(StateTransitionError):
        tuple(upload.complete())
    upload.put_part(1, b"one")
    upload.put_part(1, b"one")
    assert tuple(upload.complete()) == (b"one", b"two")
    with pytest.raises(StateTransitionError):
        tuple(upload.complete())
    with pytest.raises(StateTransitionError):
        upload.put_part(3, b"three")
    aborted = MultipartUploadBuffer()
    aborted.abort()
    with pytest.raises(StateTransitionError):
        aborted.put_part(1, b"no")
