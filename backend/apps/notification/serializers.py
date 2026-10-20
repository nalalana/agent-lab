"""
/backend/apps/notification/serializers.py
消息通知序列化器
"""
from rest_framework import serializers


class NotificationListSerializer(serializers.Serializer):
    """获取通知列表序列化器"""
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)
    status = serializers.ChoiceField(
        choices=['unread', 'read', 'all'],
        required=False,
        default='all'
    )
    type = serializers.CharField(required=False, allow_blank=True)


class NotificationMarkReadSerializer(serializers.Serializer):
    """标记通知为已读序列化器"""
    notification_id = serializers.IntegerField(min_value=1)


class NotificationMarkAllReadSerializer(serializers.Serializer):
    """标记所有通知为已读序列化器"""
    pass


class NotificationUnreadCountSerializer(serializers.Serializer):
    """获取未读通知数量序列化器"""
    pass
