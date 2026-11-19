"""The only client allowed to call the Java business API."""

from app.clients.java_backend.client import ContractRecord, ContractVersionRecord, JavaBackendClient, JavaBackendError

__all__ = ["ContractRecord", "ContractVersionRecord", "JavaBackendClient", "JavaBackendError"]
