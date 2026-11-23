"""Safe failures for long-term memory.

Messages stay fixed. They do not include memory text, secrets, or SQL.
"""


class MemoryError(Exception):
    """Base error for memory operations."""


class MemoryAccessError(MemoryError):
    """The caller did not supply a usable access context."""


class MemoryValidationError(MemoryError):
    """The memory itself cannot be stored or changed."""


class MemoryForbidden(MemoryError):
    """The context is valid and the operation is not granted."""


class MemoryNotFound(MemoryError):
    """The row is missing or not visible to this context."""


class MemoryVersionConflict(MemoryError):
    """Another update already changed this version."""
