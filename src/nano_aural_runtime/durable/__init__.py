"""Model-agnostic durable-domain contracts for Roadmap Phase 3A.

This package deliberately has no database driver or runtime-model dependency.
PostgreSQL is defined by the migration; :class:`InMemoryDurableRepository` is a
deterministic test double, not a production persistence implementation.
"""

from .domain import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactState,
    AssetKind,
    AssetRecord,
    AssetState,
    AttemptRecord,
    AttemptState,
    BlobRecord,
    BlobState,
    DeploymentRecord,
    DeploymentState,
    EventType,
    JobEventRecord,
    JobInput,
    JobRecord,
    JobState,
    WorkerRecord,
    WorkerState,
    canonical_request_sha256,
)
from .errors import (
    DurableInvariantError,
    IdempotencyConflictError,
    NotFoundError,
    StateTransitionError,
)
from .fake_worker import FakeRuntimeWorker
from .materialization import (
    AttemptInputMaterializer,
    AttemptLease,
    EvidenceDriftError,
    LeaseAuthority,
    MaterializationError,
    MaterializationIntegrityError,
    MaterializationLeaseLostError,
    MaterializationMediaError,
    MaterializedInput,
    VerifiedInputEvidence,
    WorkspaceCleanupError,
)
from .migrations import apply_postgres_migrations
from .postgres_repository import PostgresDurableRepository
from .postgres_uploads import PostgresUploadRepository
from .queue import Lease, PostgresLeaseQueue
from .queue_worker import FakeLeaseWorker, InMemoryCandidate
from .repository import DurableRepository, InMemoryDurableRepository
from .runtime_worker import (
    DurableRuntimeWorker,
    InvocationBuilder,
    RuntimeCandidate,
    WorkerProcessFatal,
)
from .storage import BlobObject, LocalBlobStore
from .upload_transports import (
    CanonicalBlobStore,
    InMemoryS3CompatibleCanonical,
    MultipartUploadBuffer,
)
from .uploads import (
    CommandMediaProbe,
    InMemoryS3CompatibleStaging,
    InMemoryUploadRepository,
    LocalStagingBlobStore,
    MediaInfo,
    UploadCliState,
    UploadMode,
    UploadSession,
    UploadState,
    UploadVerifier,
    WaveMediaProbe,
)

__all__ = [
    "ArtifactKind",
    "apply_postgres_migrations",
    "ArtifactRecord",
    "ArtifactState",
    "AssetKind",
    "AssetRecord",
    "AssetState",
    "AttemptRecord",
    "AttemptState",
    "BlobObject",
    "BlobRecord",
    "BlobState",
    "DeploymentRecord",
    "DeploymentState",
    "DurableInvariantError",
    "DurableRepository",
    "FakeRuntimeWorker",
    "EventType",
    "IdempotencyConflictError",
    "InMemoryDurableRepository",
    "JobInput",
    "JobEventRecord",
    "JobRecord",
    "JobState",
    "LocalBlobStore",
    "NotFoundError",
    "PostgresDurableRepository",
    "PostgresUploadRepository",
    "Lease",
    "PostgresLeaseQueue",
    "FakeLeaseWorker",
    "InMemoryCandidate",
    "DurableRuntimeWorker",
    "InvocationBuilder",
    "RuntimeCandidate",
    "WorkerProcessFatal",
    "StateTransitionError",
    "WorkerRecord",
    "WorkerState",
    "LocalStagingBlobStore",
    "InMemoryUploadRepository",
    "InMemoryS3CompatibleStaging",
    "MediaInfo",
    "UploadCliState",
    "UploadMode",
    "UploadSession",
    "UploadState",
    "UploadVerifier",
    "WaveMediaProbe",
    "CommandMediaProbe",
    "CanonicalBlobStore",
    "InMemoryS3CompatibleCanonical",
    "MultipartUploadBuffer",
    "canonical_request_sha256",
    "AttemptInputMaterializer",
    "AttemptLease",
    "EvidenceDriftError",
    "LeaseAuthority",
    "MaterializationError",
    "MaterializationIntegrityError",
    "MaterializationLeaseLostError",
    "MaterializationMediaError",
    "MaterializedInput",
    "VerifiedInputEvidence",
    "WorkspaceCleanupError",
]
