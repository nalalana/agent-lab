"""Approved inventory-only retention policy for enterprise verification."""

# This identifier is checked at startup. Neither inventory task deletes data.
RETENTION_POLICY_ID = (
    "terminal-documents-180d-draft-inactive-3m-inventory-"
    "verified-unlimited-v1"
)
TERMINAL_DOCUMENT_AGE_SECONDS = 180 * 86400
DRAFT_INACTIVE_MONTHS = 3
