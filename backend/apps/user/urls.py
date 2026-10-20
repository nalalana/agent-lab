"""
/backend/apps/user/urls.py
用户URL路由
"""
from django.urls import path

from apps.user import views, feedback

urlpatterns = [
    path('feedback', feedback.collection),
    path('feedback/<int:feedback_id>/images/<int:index>', feedback.image),
    # 验证码和激活
    path('email/<str:token>', views.active),
    path('get_captcha', views.get_captcha),
    
    # 手机短信验证码
    path('send_sms_code', views.send_sms_code),
    path('register_by_phone', views.register_by_phone),
    path('login_by_phone', views.login_by_phone),
    
    # 用户认证
    path('login_by_password', views.login_by_password),
    path('logout', views.logout),
    path('two-step-verify', views.two_step_verify),

    # 用户资料管理
    path('profile', views.get_profile),
    path('profile/update', views.update_profile),
    path('avatar/upload', views.upload_avatar),
    path('password/change', views.change_password),
    
    # 其他功能
    path('refresh_token', views.refresh_token),
    path('view_user', views.view_user),
    path('search', views.search_user),
    path('search-users-by-phone', views.search_users_by_phone),
    path('admin/add', views.admin_add), # 统一平台管理员授权
    path('admin/delete', views.admin_delete), # 统一平台管理员撤销
    path('quick_init_root', views.quick_init_root), # 该接口已废弃，后续不再使用
]


