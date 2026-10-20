"""
/backend/apps/notification/tasks.py
消息通知异步任务（Celery）
"""
import logging
from typing import Optional, Dict, Any, List
from celery import shared_task
from apps.user.models import User
from apps.company.models import TransparentCompany
from apps.notification.services import NotificationService

logger = logging.getLogger('touming')


@shared_task
def send_notification_task(
    user_id: int,
    type: str,
    title: str,
    content: str,
    transparent_company_id: Optional[int] = None,
    extra_data: Optional[Dict[str, Any]] = None
):
    """
    异步发送通知任务
    
    Args:
        user_id: 用户ID
        type: 通知类型
        title: 通知标题
        content: 通知内容
        transparent_company_id: 透明公司ID（可选）
        extra_data: 额外数据（可选）
    """
    try:
        user = User.objects.get(id=user_id)
        transparent_company = None
        if transparent_company_id:
            transparent_company = TransparentCompany.objects.get(id=transparent_company_id)
        
        NotificationService.create_notification(
            user=user,
            type=type,
            title=title,
            content=content,
            transparentCompany=transparent_company,
            extraData=extra_data
        )
        
        logger.info(f"[notification-task] 异步发送通知成功 user_id={user_id} type={type}")
    except User.DoesNotExist:
        logger.error(f"[notification-task] 用户不存在 user_id={user_id}")
    except TransparentCompany.DoesNotExist:
        logger.error(f"[notification-task] 透明公司不存在 transparent_company_id={transparent_company_id}")
    except Exception as e:
        logger.error(f"[notification-task] 异步发送通知失败 user_id={user_id} err={e}")


@shared_task
def send_login_notification_task(user_id: int, client_ip: Optional[str] = None):
    """
    异步发送登录通知任务
    
    Args:
        user_id: 用户ID
        client_ip: 客户端IP（可选）
    """
    try:
        user = User.objects.get(id=user_id)
        NotificationService.send_login_notification(user, client_ip)
        logger.info(f"[notification-task] 异步发送登录通知成功 user_id={user_id}")
    except User.DoesNotExist:
        logger.error(f"[notification-task] 用户不存在 user_id={user_id}")
    except Exception as e:
        logger.error(f"[notification-task] 异步发送登录通知失败 user_id={user_id} err={e}")


@shared_task
def send_security_review_notification_task(
    user_id: int,
    transparent_company_id: int,
    errors: List[str],
    repo_full_name: str
):
    """
    异步发送安全审查结果通知任务
    
    Args:
        user_id: 用户ID
        transparent_company_id: 透明公司ID
        errors: 错误列表
        repo_full_name: 仓库全名
    """
    try:
        user = User.objects.get(id=user_id)
        transparent_company = TransparentCompany.objects.get(id=transparent_company_id)
        
        NotificationService.send_security_review_notification(
            user=user,
            transparentCompany=transparent_company,
            errors=errors,
            repo_full_name=repo_full_name
        )
        
        logger.info(f"[notification-task] 异步发送安全审查通知成功 user_id={user_id} transparent_company_id={transparent_company_id}")
    except User.DoesNotExist:
        logger.error(f"[notification-task] 用户不存在 user_id={user_id}")
    except TransparentCompany.DoesNotExist:
        logger.error(f"[notification-task] 透明公司不存在 transparent_company_id={transparent_company_id}")
    except Exception as e:
        logger.error(f"[notification-task] 异步发送安全审查通知失败 user_id={user_id} err={e}")


@shared_task
def send_skill_sync_success_notification_task(
    user_id: int,
    transparent_company_id: int,
    changed_skills: List[Dict[str, Any]],
    repo_full_name: str
):
    """
    异步发送技能同步成功通知任务
    
    Args:
        user_id: 用户ID
        transparent_company_id: 透明公司ID
        changed_skills: 变更的技能列表
        repo_full_name: 仓库全名
    """
    try:
        user = User.objects.get(id=user_id)
        transparent_company = TransparentCompany.objects.get(id=transparent_company_id)
        
        NotificationService.send_skill_sync_success_notification(
            user=user,
            transparentCompany=transparent_company,
            changed_skills=changed_skills,
            repo_full_name=repo_full_name
        )
        
        logger.info(f"[notification-task] 异步发送技能同步成功通知 user_id={user_id} transparent_company_id={transparent_company_id}")
    except User.DoesNotExist:
        logger.error(f"[notification-task] 用户不存在 user_id={user_id}")
    except TransparentCompany.DoesNotExist:
        logger.error(f"[notification-task] 透明公司不存在 transparent_company_id={transparent_company_id}")
    except Exception as e:
        logger.error(f"[notification-task] 异步发送技能同步成功通知失败 user_id={user_id} err={e}")


@shared_task
def send_skill_sync_warning_notification_task(
    user_id: int,
    transparent_company_id: int,
    inconsistent_skills: List[str],
    repo_full_name: str
):
    """
    异步发送技能同步警告通知任务
    
    Args:
        user_id: 用户ID
        transparent_company_id: 透明公司ID
        inconsistent_skills: 不一致的技能列表
        repo_full_name: 仓库全名
    """
    try:
        user = User.objects.get(id=user_id)
        transparent_company = TransparentCompany.objects.get(id=transparent_company_id)
        
        NotificationService.send_skill_sync_warning_notification(
            user=user,
            transparentCompany=transparent_company,
            inconsistent_skills=inconsistent_skills,
            repo_full_name=repo_full_name
        )
        
        logger.info(f"[notification-task] 异步发送技能同步警告通知 user_id={user_id} transparent_company_id={transparent_company_id}")
    except User.DoesNotExist:
        logger.error(f"[notification-task] 用户不存在 user_id={user_id}")
    except TransparentCompany.DoesNotExist:
        logger.error(f"[notification-task] 透明公司不存在 transparent_company_id={transparent_company_id}")
    except Exception as e:
        logger.error(f"[notification-task] 异步发送技能同步警告通知失败 user_id={user_id} err={e}")
