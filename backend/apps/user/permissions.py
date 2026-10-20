"""Shared platform-administrator authorization for all administration modules."""

from apps.user.models import User


PLATFORM_ADMIN_ROLES = ("admin", "root")


def get_platform_administrator(user_or_id, *, lock=False):
    """Read the current role so a previously loaded user cannot retain revoked access."""
    user_id = getattr(user_or_id, "pk", user_or_id)
    if user_id is None:
        return None
    queryset = User.objects.filter(pk=user_id, role__in=PLATFORM_ADMIN_ROLES).only(
        "id", "uid", "username", "role",
    )
    if lock:
        queryset = queryset.select_for_update()
    return queryset.first()


def is_platform_administrator(user_or_id):
    return get_platform_administrator(user_or_id) is not None
