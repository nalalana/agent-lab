"""OpenViking-backed memory services for the unified Agent runtime."""

from .gateway import OpenVikingMemoryGateway
from .namespaces import MemoryNamespace, NamespaceResolver

__all__ = ['MemoryNamespace', 'NamespaceResolver', 'OpenVikingMemoryGateway']
