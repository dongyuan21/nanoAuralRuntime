"""Dependency-free remote client surface for nanoAuralRuntime."""

from .client import (
    HttpResponse,
    HttpTransport,
    RemoteApiError,
    RemoteClient,
    RemoteConflict,
    RemoteEventPage,
    RemoteIntegrityError,
    RemoteNotFound,
    UrllibTransport,
)

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "RemoteApiError",
    "RemoteClient",
    "RemoteConflict",
    "RemoteIntegrityError",
    "RemoteNotFound",
    "RemoteEventPage",
    "UrllibTransport",
]
