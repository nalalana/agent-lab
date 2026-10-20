"""
/backend/apps/notification/services.py
消息通知服务类
"""
import json
import time
import logging
from typing import Optional, Dict, Any, List
from django.db.models import Q, Count
from apps.notification.models import Notification
from apps.user.models import User
from apps.company.models import TransparentCompany

logger = logging.getLogger('touming')


class NotificationService:
    """消息通知服务"""
    
    @staticmethod
    def create_notification(
        user: User,
        type: str,
        title: str,
        content: str,
        transparentCompany: Optional[TransparentCompany] = None,
        extraData: Optional[Dict[str, Any]] = None,
        deduplication_key: Optional[str] = None,
    ) -> Notification:
        """
        创建通知
        
        Args:
            user: 接收通知的用户
            type: 通知类型
            title: 通知标题
            content: 通知内容
            transparentCompany: 关联的透明公司（可选）
            extraData: 额外数据（可选）
            
        Returns:
            创建的 Notification 对象
        """
        values = {
            'user': user,
            'type': type,
            'title': title,
            'content': content,
            'transparentCompany': transparentCompany,
            'extraData': json.dumps(extraData) if extraData else None,
            'created_at': str(time.time())[:20],
            'status': Notification.STATUS_UNREAD,
        }
        key = str(deduplication_key or '').strip() or None
        if key:
            notification, _created = Notification.objects.get_or_create(
                deduplication_key=key,
                defaults=values,
            )
        else:
            notification = Notification.objects.create(**values)
        
        logger.info(f"[notification] 创建通知成功 user={user.uid} type={type} notification_id={notification.id}")
        return notification
    
    @staticmethod
    def get_notifications(
        user: User,
        limit: int = 20,
        offset: int = 0,
        status: str = 'all',
        type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取用户的通知列表
        
        Args:
            user: 用户对象
            limit: 每页数量
            offset: 偏移量
            status: 状态筛选（unread/read/all）
            type: 类型筛选（可选）
            
        Returns:
            包含 total, items, hasMore 的字典
        """
        queryset = Notification.objects.filter(user=user)
        
        # 状态筛选
        if status == 'unread':
            queryset = queryset.filter(status=Notification.STATUS_UNREAD)
        elif status == 'read':
            queryset = queryset.filter(status=Notification.STATUS_READ)
        # status == 'all' 时不筛选
        
        # 类型筛选
        if type:
            queryset = queryset.filter(type=type)
        
        total = queryset.count()
        notifications = list(queryset[offset:offset + limit])
        
        return {
            'total': total,
            'items': [n.get_info() for n in notifications],
            'hasMore': (offset + limit) < total
        }
    
    @staticmethod
    def mark_as_read(user: User, notification_id: int) -> tuple[bool, str]:
        """
        标记通知为已读
        
        Args:
            user: 用户对象
            notification_id: 通知ID
            
        Returns:
            (成功标志, 错误信息)
        """
        try:
            notification = Notification.objects.get(id=notification_id, user=user)
            if notification.status == Notification.STATUS_READ:
                return True, '通知已经是已读状态'
            
            notification.mark_as_read()
            logger.info(f"[notification] 标记已读成功 user={user.uid} notification_id={notification_id}")
            return True, '标记成功'
        except Notification.DoesNotExist:
            logger.warning(f"[notification] 通知不存在 user={user.uid} notification_id={notification_id}")
            return False, '通知不存在'
        except Exception as e:
            logger.error(f"[notification] 标记已读失败 user={user.uid} notification_id={notification_id} err={e}")
            return False, f'标记失败: {str(e)}'
    
    @staticmethod
    def mark_all_as_read(user: User) -> tuple[bool, str]:
        """
        标记用户所有未读通知为已读
        
        Args:
            user: 用户对象
            
        Returns:
            (成功标志, 错误信息)
        """
        try:
            count = Notification.objects.filter(
                user=user,
                status=Notification.STATUS_UNREAD
            ).update(
                status=Notification.STATUS_READ,
                read_at=str(time.time())[:20]
            )
            
            logger.info(f"[notification] 标记全部已读成功 user={user.uid} count={count}")
            return True, f'已标记 {count} 条通知为已读'
        except Exception as e:
            logger.error(f"[notification] 标记全部已读失败 user={user.uid} err={e}")
            return False, f'标记失败: {str(e)}'
    
    @staticmethod
    def get_unread_count(user: User) -> int:
        """
        获取用户未读通知数量
        
        Args:
            user: 用户对象
            
        Returns:
            未读通知数量
        """
        return Notification.objects.filter(
            user=user,
            status=Notification.STATUS_UNREAD
        ).count()
    
    @staticmethod
    def send_login_notification(user: User, client_ip: Optional[str] = None) -> Notification:
        """
        发送登录通知
        
        Args:
            user: 用户对象
            client_ip: 客户端IP（可选）
            
        Returns:
            创建的 Notification 对象
        """
        # 获取 IP 地理位置信息
        ip_info = ""
        if client_ip:
            try:
                from common.utils.ip_location_utils import format_ip_location
                ip_info = format_ip_location(client_ip)
            except Exception as e:
                logger.warning(f"[notification] 获取 IP 地理位置失败 ip={client_ip} err={e}")
                # 如果获取地理位置失败，回退到只显示 IP
                ip_info = f"（IP: {client_ip}）"
        
        title = "账户登录通知"
        content = f"您的账户在 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} 成功登录{ip_info}。"
        
        notification = NotificationService.create_notification(
            user=user,
            type=Notification.TYPE_LOGIN,
            title=title,
            content=content,
            extraData={'client_ip': client_ip} if client_ip else None
        )
        
        # 通过 WebSocket 实时推送通知
        try:
            from apps.notification.consumers import send_notification_via_websocket
            send_notification_via_websocket(
                user_id=user.id,
                notification_data=notification.get_info()
            )
        except Exception as e:
            logger.warning(f"[notification] WebSocket 推送失败 user_id={user.id} err={e}")
        
        return notification
    
    @staticmethod
    def send_security_review_notification(
        user: User,
        transparentCompany: TransparentCompany,
        errors: List[str],
        repo_full_name: str
    ) -> Notification:
        """
        发送安全审查结果通知
        
        Args:
            user: 用户对象
            transparentCompany: 透明公司对象
            errors: 错误列表
            repo_full_name: 仓库全名
            
        Returns:
            创建的 Notification 对象
        """
        title = "安全审查未通过"
        content = f"您的透明公司 {transparentCompany.company.company_name} 的技能同步安全审查未通过。\n\n"
        content += f"仓库：{repo_full_name}\n"
        content += f"错误信息：\n" + "\n".join([f"- {error}" for error in errors[:10]])
        if len(errors) > 10:
            content += f"\n... 还有 {len(errors) - 10} 条错误"
        
        notification = NotificationService.create_notification(
            user=user,
            type=Notification.TYPE_SECURITY_REVIEW,
            title=title,
            content=content,
            transparentCompany=transparentCompany,
            extraData={
                'repo_full_name': repo_full_name,
                'errors': errors,
                'error_count': len(errors)
            }
        )
        
        # 通过 WebSocket 实时推送通知
        try:
            from apps.notification.consumers import send_notification_via_websocket
            send_notification_via_websocket(
                user_id=user.id,
                notification_data=notification.get_info()
            )
        except Exception as e:
            logger.warning(f"[notification] WebSocket 推送失败 user_id={user.id} err={e}")
        
        return notification
    
    @staticmethod
    def send_skill_sync_success_notification(
        user: User,
        transparentCompany: TransparentCompany,
        changed_skills: List[Dict[str, Any]],
        repo_full_name: str
    ) -> Notification:
        """
        发送技能同步成功通知
        
        Args:
            user: 用户对象
            transparentCompany: 透明公司对象
            changed_skills: 变更的技能列表
            repo_full_name: 仓库全名
            
        Returns:
            创建的 Notification 对象
        """
        title = "技能同步成功"
        skill_count = len(changed_skills)
        content = f"您的透明公司 {transparentCompany.company.company_name} 的技能同步成功。\n\n"
        content += f"仓库：{repo_full_name}\n"
        content += f"同步了 {skill_count} 个技能"
        
        notification = NotificationService.create_notification(
            user=user,
            type=Notification.TYPE_SKILL_SYNC_SUCCESS,
            title=title,
            content=content,
            transparentCompany=transparentCompany,
            extraData={
                'repo_full_name': repo_full_name,
                'changed_skills': changed_skills,
                'skill_count': skill_count
            }
        )
        
        # 通过 WebSocket 实时推送通知
        try:
            from apps.notification.consumers import send_notification_via_websocket
            send_notification_via_websocket(
                user_id=user.id,
                notification_data=notification.get_info()
            )
        except Exception as e:
            logger.warning(f"[notification] WebSocket 推送失败 user_id={user.id} err={e}")
        
        return notification
    
    @staticmethod
    def send_skill_sync_warning_notification(
        user: User,
        transparentCompany: TransparentCompany,
        inconsistent_skills: List[str],
        repo_full_name: str
    ) -> Notification:
        """
        发送技能同步警告通知
        
        Args:
            user: 用户对象
            transparentCompany: 透明公司对象
            inconsistent_skills: 不一致的技能列表
            repo_full_name: 仓库全名
            
        Returns:
            创建的 Notification 对象
        """
        title = "技能同步警告"
        content = f"您的透明公司 {transparentCompany.company.company_name} 的技能同步出现警告。\n\n"
        content += f"仓库：{repo_full_name}\n"
        content += f"manifest 与 skills 目录不一致的技能：{', '.join(inconsistent_skills)}"
        
        notification = NotificationService.create_notification(
            user=user,
            type=Notification.TYPE_SKILL_SYNC_WARNING,
            title=title,
            content=content,
            transparentCompany=transparentCompany,
            extraData={
                'repo_full_name': repo_full_name,
                'inconsistent_skills': inconsistent_skills
            }
        )
        
        # 通过 WebSocket 实时推送通知
        try:
            from apps.notification.consumers import send_notification_via_websocket
            send_notification_via_websocket(
                user_id=user.id,
                notification_data=notification.get_info()
            )
        except Exception as e:
            logger.warning(f"[notification] WebSocket 推送失败 user_id={user.id} err={e}")
        
        return notification

    @staticmethod
    def send_runtime_config_alert_notification(
        user: User,
        transparentCompany: Optional[TransparentCompany],
        company_name: str,
        issue_sections: List[str],
        event_data: Dict[str, Any]
    ) -> Notification:
        """
        发送运行配置异常提醒。
        """
        section_text = "、".join(issue_sections[:8]) if issue_sections else "运行配置"
        title = "配置异常提醒"
        content = f"{company_name} 的配置状态需要处理。\n\n异常范围：{section_text}"
        if len(issue_sections) > 8:
            content += f"\n另有 {len(issue_sections) - 8} 项未显示"

        notification = NotificationService.create_notification(
            user=user,
            type=Notification.TYPE_RUNTIME_CONFIG_ALERT,
            title=title,
            content=content,
            transparentCompany=transparentCompany,
            extraData={
                "issue_sections": issue_sections,
                "event": event_data,
            }
        )

        try:
            from apps.notification.consumers import send_notification_via_websocket
            send_notification_via_websocket(
                user_id=user.id,
                notification_data=notification.get_info()
            )
        except Exception as e:
            logger.warning(f"[notification] WebSocket 推送失败 user_id={user.id} err={e}")

        return notification
