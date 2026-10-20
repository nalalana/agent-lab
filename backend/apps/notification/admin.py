"""
/backend/apps/notification/admin.py
消息通知管理后台
"""
from django.contrib import admin
from apps.notification.models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """通知管理"""
    list_display = ['id', 'user', 'type', 'title', 'status', 'created_at']
    list_filter = ['type', 'status', 'created_at']
    search_fields = ['title', 'content', 'user__username', 'user__uid']
    readonly_fields = ['created_at', 'read_at']
    ordering = ['-id']
    
    fieldsets = (
        ('基本信息', {
            'fields': ('user', 'type', 'title', 'content', 'status')
        }),
        ('关联信息', {
            'fields': ('transparentCompany', 'extraData')
        }),
        ('时间信息', {
            'fields': ('created_at', 'read_at')
        }),
    )
