from apps.user.models import User

def get_user_from_request(request):
    """从请求中获取用户对象"""
    user_id = getattr(request, 'user_id', None)
    if not user_id:
        return None
    try:
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        return None