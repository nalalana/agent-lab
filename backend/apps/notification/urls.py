"""
/backend/apps/notification/urls.py
消息通知URL路由
"""
from django.urls import path
from apps.notification import views

urlpatterns = [
    path('list', views.notification_list, name='notification-list'),
    path('mark-read', views.notification_mark_read, name='notification-mark-read'),
    path('mark-all-read', views.notification_mark_all_read, name='notification-mark-all-read'),
    path('unread-count', views.notification_unread_count, name='notification-unread-count'),
]
