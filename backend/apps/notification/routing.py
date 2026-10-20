"""
/backend/apps/notification/routing.py
消息通知 WebSocket 路由配置
"""
from django.urls import re_path
from apps.notification import consumers

websocket_urlpatterns = [
    re_path(r'^ws/notification/?$', consumers.NotificationConsumer.as_asgi()),
]
