"""Concrete, dependency-injected Phase 3E authentication and API wiring."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional, Protocol

from .api import ApplicationApi
from .application import (
    ApplicationService,
    AssetUploadWorkflow,
    AuthenticationFailed,
    Authenticator,
    AuthorizationPolicy,
    JobApplicationRepository,
    Principal,
    VisibleArtifactCatalog,
)


@dataclass(frozen=True)
class TokenGrant:
    """A configured token digest and its application grants.

    Only a SHA-256 digest is retained, so application configuration need not
    keep the bearer token in a long-lived Python object.
    """

    token_sha256: str
    subject: str
    scopes: FrozenSet[str]
    namespaces: FrozenSet[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        object.__setattr__(self, "namespaces", frozenset(self.namespaces))
        if (
            not isinstance(self.token_sha256, str)
            or len(self.token_sha256) != 64
            or self.token_sha256 != self.token_sha256.lower()
        ):
            raise ValueError("token_sha256 must be a lowercase full SHA-256")
        try:
            int(self.token_sha256, 16)
        except ValueError as error:
            raise ValueError("token_sha256 must be hexadecimal") from error
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("token subject must be non-empty")
        if not self.scopes or any(
            not isinstance(item, str) or not item.strip() for item in self.scopes
        ):
            raise ValueError("token scopes must be non-empty strings")
        if not self.namespaces or any(
            not isinstance(item, str) or not item.strip() for item in self.namespaces
        ):
            raise ValueError("token namespaces must be non-empty strings")

    @classmethod
    def from_token(
        cls,
        token: str,
        subject: str,
        *,
        scopes: Iterable[str],
        namespaces: Iterable[str],
    ) -> "TokenGrant":
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError("token must be non-empty and at most 4096 characters")
        return cls(
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            subject,
            frozenset(scopes),
            frozenset(namespaces),
        )


class StaticTokenAuthenticator(Authenticator):
    """Authenticate configured bearer digests using constant-time comparison."""

    def __init__(self, grants: Iterable[TokenGrant]) -> None:
        self._grants = tuple(grants)
        if not self._grants:
            raise ValueError("at least one token grant is required")
        if len({grant.token_sha256 for grant in self._grants}) != len(self._grants):
            raise ValueError("bearer token digests must be unique")

    def authenticate(self, authorization: str) -> Principal:
        if (
            not isinstance(authorization, str)
            or len(authorization) > 7 + 4096
            or not authorization.startswith("Bearer ")
        ):
            raise AuthenticationFailed("invalid authorization")
        token = authorization[7:]
        if not token or token != token.strip() or any(character.isspace() for character in token):
            raise AuthenticationFailed("invalid authorization")
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        for grant in self._grants:
            if hmac.compare_digest(candidate, grant.token_sha256):
                matched = grant
        if matched is None:
            raise AuthenticationFailed("invalid authorization")
        return Principal(matched.subject, matched)


class StaticAuthorizationPolicy(AuthorizationPolicy):
    """Scope and namespace policy derived from configured token grants."""

    def __init__(self, grants: Iterable[TokenGrant]) -> None:
        self._grants = {id(grant): grant for grant in grants}

    def has_scope(self, principal: Principal, scope: str) -> bool:
        grant = self._grant(principal)
        return grant is not None and (scope in grant.scopes or "*" in grant.scopes)

    def allows_namespace(self, principal: Principal, namespace_id: str) -> bool:
        grant = self._grant(principal)
        return grant is not None and (namespace_id in grant.namespaces or "*" in grant.namespaces)

    def _grant(self, principal: Principal) -> Optional[TokenGrant]:
        context = principal._authorization_context
        grant = self._grants.get(id(context))
        if grant is None or context is not grant or principal.subject != grant.subject:
            return None
        return grant


class ApplicationDependencies(Protocol):
    """Deployment wiring boundary; implementations own connection lifetimes."""

    @property
    def repository(self) -> JobApplicationRepository: ...

    @property
    def artifacts(self) -> VisibleArtifactCatalog: ...

    @property
    def authenticator(self) -> Authenticator: ...

    @property
    def authorization(self) -> AuthorizationPolicy: ...

    @property
    def uploads(self) -> Optional[AssetUploadWorkflow]: ...


def build_application(dependencies: ApplicationDependencies) -> ApplicationApi:
    service = ApplicationService(
        dependencies.repository,
        dependencies.artifacts,
        dependencies.authorization,
        dependencies.uploads,
    )
    return ApplicationApi(service, dependencies.authenticator)
