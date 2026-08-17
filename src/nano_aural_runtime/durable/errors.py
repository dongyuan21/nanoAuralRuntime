"""Errors for durable-domain policy violations."""


class DurableError(Exception):
    """Base class for durable-domain failures."""


class DurableInvariantError(DurableError):
    """An operation would violate a durable system invariant."""


class StateTransitionError(DurableInvariantError):
    """A state transition is invalid for the current durable entity state."""


class IdempotencyConflictError(DurableInvariantError):
    """An idempotency key was reused for a different canonical request."""


class NotFoundError(DurableError):
    """A requested durable entity does not exist."""
