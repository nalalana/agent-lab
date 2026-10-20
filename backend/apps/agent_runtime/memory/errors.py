"""Safe internal exceptions for memory authorization and transport failures."""


class MemoryGatewayError(RuntimeError):
    code = 'MEMORY_ERROR'
    retryable = False


class MemoryPermissionDenied(MemoryGatewayError):
    code = 'MEMORY_PERMISSION_DENIED'


class MemoryConfigurationError(MemoryGatewayError):
    code = 'MEMORY_CONFIGURATION_ERROR'


class OpenVikingUnavailable(MemoryGatewayError):
    code = 'MEMORY_UNAVAILABLE'
    retryable = True


class OpenVikingAuthenticationError(MemoryGatewayError):
    code = 'MEMORY_AUTH_FAILED'


class OpenVikingResponseError(MemoryGatewayError):
    code = 'MEMORY_PROVIDER_ERROR'


class OpenVikingTaskPending(MemoryGatewayError):
    code = 'MEMORY_COMMIT_PENDING'
    retryable = True


class OpenVikingExtractionFailed(MemoryGatewayError):
    code = 'MEMORY_EXTRACTION_FAILED'
