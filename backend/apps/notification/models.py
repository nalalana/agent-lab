"""
/backend/apps/notification/models.py
消息通知模型
"""
from django.db import models
from apps.user.models import User
from apps.company.models import TransparentCompany
import time


class Notification(models.Model):
    """
    消息通知表
    
    用于存储各种类型的通知消息，如：
    - 账户登录通知
    - 审查结果通知
    - 技能同步结果通知
    """
    
    # 通知类型
    TYPE_LOGIN = 'login'
    TYPE_SECURITY_REVIEW = 'security_review'
    TYPE_SKILL_SYNC_SUCCESS = 'skill_sync_success'
    TYPE_SKILL_SYNC_FAILED = 'skill_sync_failed'
    TYPE_SKILL_SYNC_WARNING = 'skill_sync_warning'
    TYPE_RUNTIME_CONFIG_ALERT = 'runtime_config_alert'
    TYPE_ENTERPRISE_VERIFICATION = 'enterprise_verification'
    TYPE_CHOICES = [
        (TYPE_LOGIN, '登录通知'),
        (TYPE_SECURITY_REVIEW, '安全审查通知'),
        (TYPE_SKILL_SYNC_SUCCESS, '技能同步成功'),
        (TYPE_SKILL_SYNC_FAILED, '技能同步失败'),
        (TYPE_SKILL_SYNC_WARNING, '技能同步警告'),
        (TYPE_RUNTIME_CONFIG_ALERT, '配置异常提醒'),
        (TYPE_ENTERPRISE_VERIFICATION, '企业认证通知'),
    ]
    
    # 通知状态
    STATUS_UNREAD = 'unread'
    STATUS_READ = 'read'
    STATUS_CHOICES = [
        (STATUS_UNREAD, '未读'),
        (STATUS_READ, '已读'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    
    # 接收通知的用户
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='notifications')
    
    # 通知类型
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    
    # 通知标题
    title = models.CharField(max_length=255)
    
    # 通知内容
    content = models.TextField()
    
    # 通知状态（未读/已读）
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UNREAD)
    
    # 关联的透明公司（可选，某些通知可能关联到特定公司）
    transparentCompany = models.ForeignKey(
        to=TransparentCompany, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True,
        related_name='notifications'
    )
    
    # 额外数据（JSON 字符串，用于存储通知相关的额外信息）
    extraData = models.TextField(null=True, blank=True)

    # Optional idempotency key for outbox-backed producers.
    deduplication_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        unique=True,
    )
    
    # 创建时间（时间戳字符串，保持与其它表一致风格）
    created_at = models.CharField(max_length=20)
    
    # 阅读时间（时间戳字符串）
    read_at = models.CharField(max_length=20, null=True, blank=True)
    
    class Meta:
        db_table = 'notification_notification'
        ordering = ['-id']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', 'type']),
            models.Index(fields=['created_at']),
        ]
    
    def get_info(self):
        """获取通知信息"""
        return {
            'id': self.id,
            'user': self.user.get_info() if self.user else None,
            'type': self.type,
            'title': self.title,
            'content': self.content,
            'status': self.status,
            'transparentCompany': self.transparentCompany.get_info() if self.transparentCompany else None,
            'extraData': self.extraData,
            'created_at': self.created_at,
            'read_at': self.read_at,
        }
    
    def mark_as_read(self):
        """标记为已读"""
        self.status = self.STATUS_READ
        self.read_at = str(time.time())[:20]
        self.save()
