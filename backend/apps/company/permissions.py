"""Domain-level permissions for pre-verification company drafts.

Draft access is intentionally separate from IAM.  ``Company.account`` records
who created a company; it grants only the narrow pre-verification capabilities
that explicitly call :func:`can_access_company_draft`.
"""

from typing import Optional

from apps.company.models import Company
from apps.iam.models import Grant, Principal, Resource, Tenant
from apps.iam.services import is_granted
from apps.user.models import User


def can_access_company_draft(company: Company, user: Optional[User]) -> bool:
    """Return whether ``user`` is the creator of this unverified company.

    This helper must never be used by formal management operations.  Member,
    resource, transparent-company, runtime, and permission management continue
    to require IAM grants.
    """

    return bool(
        user
        and company.account_id == user.id
        and company.verification_status != Company.VERIFICATION_STATUS_VERIFIED
    )


def is_company_verified_for_formal_access(company: Company) -> bool:
    """Return whether the current Company state permits formal/public access.

    Suspension preserves the historical verified fact while removing its
    effectiveness from every formal/public capability that uses this gate.
    """

    return (
        company.verification_status == Company.VERIFICATION_STATUS_VERIFIED
        and company.verification_suspended_at is None
        and company.service_suspended_at is None
    )


def has_company_iam_permission(
    company: Company,
    user: Optional[User],
    permission: int = Grant.PERM_ADMIN,
) -> bool:
    """Check an existing company grant without creating IAM state."""

    return has_iam_permission(
        user,
        Resource.TYPE_COMPANY,
        company.cid,
        permission,
    )


def has_iam_permission(
    user: Optional[User],
    resource_type: str,
    external_id: str,
    permission: int,
) -> bool:
    """Check an existing IAM grant without materializing lookup objects."""

    if not user:
        return False
    principal = Principal.objects.filter(
        type=Principal.TYPE_USER,
        external_id=user.uid,
    ).first()
    if principal is None:
        return False
    resource = Resource.objects.filter(
        type=resource_type,
        external_id=external_id,
    ).first()
    if resource is None:
        return False
    return is_granted(principal, resource, permission)


def can_access_formal_company(
    company: Company,
    user: Optional[User],
    permission: int = Grant.PERM_ADMIN,
) -> bool:
    """Require both a verified company and an existing IAM permission."""

    if not is_company_verified_for_formal_access(company) or not user:
        return False
    if permission >= Grant.PERM_SUPER_ADMIN:
        tenant = Tenant.objects.filter(
            type=Tenant.TYPE_ORGANIZATION,
            external_id=f'organization:{company.cid}',
        ).first()
        if tenant is not None:
            return (
                tenant.status == Tenant.STATUS_ACTIVE
                and tenant.super_admin_user_id == user.id
            )
    return has_company_iam_permission(company, user, permission)
