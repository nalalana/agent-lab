import ast
import base64
import time
import logging
from collections import Counter

from django.db.models import Count

from apps.company.models import Company
from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService
from apps.opengate.models import Gates
from apps.ops.models import CompanyFollow, GateFollow, Partner, PopularCompany, PopularGate
from common.utils.Tengcentcos_utils import put_object, get_moderated_image_presigned_url


logger = logging.getLogger('touming')


def _ip_suffix(client_ip: str | None) -> str:
    return f" ip={client_ip}" if client_ip else ""


class OpsService:
    @staticmethod
    def list_hot_gate(client_ip: str | None = None):
        now = time.time()
        week_ago = now - 7 * 24 * 60 * 60
        try:
            follow_gates = GateFollow.objects.filter(created_at__gte=week_ago)
            gates = [str(gate.target) for gate in follow_gates]
            gate_count = Counter(gates)
            hot_gates = gate_count.most_common(10)
            hot_gates_list = []
            for gate in hot_gates:
                gate = ast.literal_eval(gate)
                hot_gates_list.append({
                    'gate_name': gate['gatename'],
                    'gate_id': gate['id'],
                    'gate_description': gate['description'],
                    'gate_followers': gate[1],
                })
            return hot_gates_list
        except Exception as e:
            logger.error(f"list_hot_gate failed:{_ip_suffix(client_ip)} err={e}")
            return []

    @staticmethod
    def list_hot_company(client_ip: str | None = None):
        now = time.time()
        week_ago = now - 7 * 24 * 60 * 60
        try:
            hot_companies = list(
                CompanyFollow.objects.filter(
                    created_at__gte=week_ago,
                    target__verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                    target__verification_suspended_at__isnull=True,
                    target__service_suspended_at__isnull=True,
                )
                .values('target_id')
                .annotate(follower_count=Count('id'))
                .order_by('-follower_count', 'target_id')[:10]
            )
            companies = {
                company.id: company
                for company in Company.objects.filter(
                    id__in=[item['target_id'] for item in hot_companies],
                    verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                    verification_suspended_at__isnull=True,
                    service_suspended_at__isnull=True,
                )
            }
            hot_companies_list = []
            for item in hot_companies:
                company = companies.get(item['target_id'])
                if company is None:
                    continue
                hot_companies_list.append({
                    'company_id': company.id,
                    'company_name': company.company_name,
                    'company_logo': get_moderated_image_presigned_url(company.logo) if company.logo else None,
                    'company_description': company.description,
                    'company_followers': item['follower_count'],
                })
            return hot_companies_list
        except Exception as e:
            logger.error(f"list_hot_company failed:{_ip_suffix(client_ip)} err={e}")
            return []

    @staticmethod
    def get_popular_gate(limit: int = 100):
        popular_gates = PopularGate.objects.filter(limit=limit, order_by='-id')
        result = []
        for gate in popular_gates:
            result.append({
                'gate_id': gate.gate.id,
                'gate_name': gate.gate.gatename,
                'gate_description': gate.gate.description,
                'gate_followers': gate.followers,
            })
        return result

    @staticmethod
    def get_popular_company(limit: int = 100):
        popular_companies = (
            PopularCompany.objects.filter(
                company__verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                company__verification_suspended_at__isnull=True,
                company__service_suspended_at__isnull=True,
            )
            .select_related('company')
            .order_by('-id')[:limit]
        )
        result = []
        for popular in popular_companies:
            company = popular.company
            result.append({
                'company_id': company.id,
                'company_name': company.company_name,
                'company_logo': get_moderated_image_presigned_url(company.logo) if company.logo else None,
                'company_description': company.description,
                'company_followers': company.followers,
            })
        return result

    @staticmethod
    def get_partners(limit: int = 100):
        partners = Partner.objects.filter(limit=limit, order_by='-id')
        result = []
        for partner in partners:
            result.append({
                'partner_id': partner.id,
                'partner_name': partner.name,
                'partner_logo': get_moderated_image_presigned_url(partner.logo) if partner.logo else None,
                'partner_description': partner.description,
                'partner_website': partner.website,
            })
        return result

    @staticmethod
    def set_popular_gate(gate_ids: list[int], client_ip: str | None = None):
        try:
            for gate_id in gate_ids:
                gate = Gates.objects.get(id=gate_id)
                PopularGate.objects.create(gate=gate)
            return True
        except Exception as e:
            logger.error(f"set_popular_gate failed:{_ip_suffix(client_ip)} err={e}")
            return False

    @staticmethod
    def set_popular_company(company_ids: list[int], client_ip: str | None = None):
        try:
            for company_id in company_ids:
                company = Company.objects.get(
                    id=company_id,
                    verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                    verification_suspended_at__isnull=True,
                    service_suspended_at__isnull=True,
                )
                PopularCompany.objects.create(company=company)
            return True
        except Exception as e:
            logger.error(f"set_popular_company failed:{_ip_suffix(client_ip)} err={e}")
            return False

    @staticmethod
    def create_partner(logo: str | None, name: str, description: str | None, website: str | None, client_ip: str | None = None):
        try:
            # 处理 base64 图片
            if logo is not None and isinstance(logo, str):
                if ',' in logo:
                    header, file_data_b64 = logo.split(',', 1)
                else:
                    file_data_b64 = logo
                file_data = base64.b64decode(file_data_b64)
                file_name = f"partner/{name}/logo_{int(time.time())}.png"
                logo = put_object(file_data, file_name)
                ImageModerationService.register_uploaded_image(
                    logo,
                    source=ImageModerationAsset.SOURCE_OPS_PARTNER_LOGO,
                    source_field="logo",
                    owner_type="ops_partner",
                    owner_id=name,
                    mime_type="image/png",
                    size=len(file_data),
                )
            Partner.objects.create(
                logo=logo or '',
                name=name,
                description=description or '',
                website=website or '',
                created_at=float(time.time()),
                updated_at=float(time.time()),
            )
            return True
        except Exception as e:
            logger.error(f"create_partner failed:{_ip_suffix(client_ip)} err={e}")
            return False


