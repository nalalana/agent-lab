import time
import logging
from django.db import transaction
from apps.follow.models import FollowingGate, FollowingCompany
from apps.user.models import User
from apps.opengate.models import Gates
from apps.company.models import Company
from common.utils.Tengcentcos_utils import get_avatar_presigned_url

logger = logging.getLogger('touming')


class FollowingService:
    """关注服务类"""
       
    @staticmethod
    def follow_gate(user: User, gate_id: str):
        """关注Gate"""
        try:
            gate = Gates.objects.get(gid=gate_id)
            
            # 检查是否已经关注
            if FollowingGate.objects.filter(user=user, following_opengate=gate).exists():
                return False, "已经关注过该应用"
            
            # 创建关注关系
            FollowingGate.objects.create(
                user=user,
                following_opengate=gate,
                updated_at=str(time.time())[:20],
            )
            
            # 更新关注计数
            gate.followers = (gate.followers or 0) + 1
            gate.save()
            
            return True, "关注成功"
        except Gates.DoesNotExist:
            return False, "应用不存在"
        except Exception as e:
            logger.error(f"关注应用失败: {e}")
            return False, "关注失败"
    
    @staticmethod
    def unfollow_gate(user: User, gate_id: str):
        """取消关注Gate"""
        try:
            gate = Gates.objects.get(gid=gate_id)
            
            # 删除关注关系
            following_relation = FollowingGate.objects.filter(
                user=user, 
                following_opengate=gate
            ).first()
            
            if not following_relation:
                return False, "未关注该应用"
            
            following_relation.delete()
            
            # 更新关注计数
            gate.followers = max((gate.followers or 0) - 1, 0)
            gate.save()
            
            return True, "取消关注成功"
        except Gates.DoesNotExist:
            return False, "应用不存在"
        except Exception as e:
            logger.error(f"取消关注失败: {e}")
            return False, "取消关注失败"
    
    
    @staticmethod
    def is_following_gate(user: User, gid: str):
        """检查当前用户是否已关注 Gate。"""
        try:
            gate = Gates.objects.get(gid=gid)
        except Gates.DoesNotExist:
            return False, "应用不存在"
        try:
            return True, FollowingGate.objects.filter(
                user=user,
                following_opengate=gate,
            ).exists()
        except Exception as exc:
            logger.error(f"检查Gate关注状态失败: {exc}")
            return False, "检查Gate关注状态失败"


    @staticmethod
    def follow_company(user: User, cid: str):
        """关注已认证公司。"""
        try:
            with transaction.atomic():
                company = Company.objects.select_for_update().get(
                    cid=cid,
                    verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                    verification_suspended_at__isnull=True,
                    service_suspended_at__isnull=True,
                )
                if FollowingCompany.objects.filter(
                    user=user,
                    following_company=company,
                ).exists():
                    return False, "已经关注过该公司"
                FollowingCompany.objects.create(
                    user=user,
                    following_company=company,
                    updated_at=str(time.time())[:20],
                )
                company.followers = max(int(company.followers or 0), 0) + 1
                company.save(update_fields=['followers'])
            return True, "关注成功"
        except Company.DoesNotExist:
            return False, "公司不存在"
        except Exception as exc:
            logger.error(f"关注公司失败: {exc}")
            return False, "关注失败"


    @staticmethod
    def unfollow_company(user: User, cid: str):
        """取消关注公司。"""
        try:
            with transaction.atomic():
                company = Company.objects.select_for_update().get(cid=cid)
                deleted, _ = FollowingCompany.objects.filter(
                    user=user,
                    following_company=company,
                ).delete()
                if not deleted:
                    return False, "未关注该公司"
                company.followers = max(int(company.followers or 0) - 1, 0)
                company.save(update_fields=['followers'])
            return True, "取消关注成功"
        except Company.DoesNotExist:
            return False, "公司不存在"
        except Exception as exc:
            logger.error(f"取消关注失败: {exc}")
            return False, "取消关注失败"


    @staticmethod
    def is_following_company(user: User, cid: str):
        """检查当前用户是否已关注 Company。"""
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, "公司不存在"
        try:
            return True, FollowingCompany.objects.filter(
                user=user,
                following_company=company,
            ).exists()
        except Exception as exc:
            logger.error(f"检查Company关注状态失败: {exc}")
            return False, "检查Company关注状态失败"


    @staticmethod
    def get_following_gates(user: User, limit=20, offset=0):
        """获取当前用户关注的 Gate 列表。"""
        try:
            following_gates = FollowingGate.objects.filter(
                user=user,
            ).select_related('following_opengate')
            total = following_gates.count()
            following_gates = following_gates.order_by('id')[
                int(offset):int(offset) + int(limit)
            ]
            has_more = total > int(offset) + int(limit)
            gates_list = [
                relation.following_opengate.get_info()
                for relation in following_gates
            ]
            return True, {
                'gates': gates_list,
                'has_more': has_more,
                'total': total,
            }
        except Exception as exc:
            logger.error(f"获取关注的Gate列表失败: {exc}")
            return False, f"获取关注的Gate列表失败: {exc}"


    @staticmethod
    def get_following_companies(user: User, limit=20, offset=0):
        """获取当前用户关注的已认证 Company 列表。"""
        try:
            from apps.company.services import CompanyService

            following_companies = FollowingCompany.objects.filter(
                user=user,
                following_company__verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                following_company__verification_suspended_at__isnull=True,
                following_company__service_suspended_at__isnull=True,
            ).select_related('following_company')
            total = following_companies.count()
            following_companies = following_companies.order_by('id')[
                int(offset):int(offset) + int(limit)
            ]
            has_more = total > int(offset) + int(limit)
            companies_list = [
                CompanyService._public_company_info(relation.following_company)
                for relation in following_companies
            ]
            return True, {
                'companies': companies_list,
                'has_more': has_more,
                'total': total,
            }
        except Exception as exc:
            logger.error(f"获取关注的Company列表失败: {exc}")
            return False, f"获取关注的Company列表失败: {exc}"
