"""
/backend/apps/notification/apps.py
消息通知应用配置
"""
from django.apps import AppConfig


class NotificationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notification'
