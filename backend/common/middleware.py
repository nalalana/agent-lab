import logging

from django.db.utils import OperationalError
from django.http import HttpResponseForbidden, HttpResponseServerError, JsonResponse
from django.conf import settings
import ipaddress

from common.db_bootstrap import ensure_runtime_database_ready, is_missing_database_error, should_attempt_runtime_db_bootstrap


logger = logging.getLogger("touming")


class AdminIPWhitelistMiddleware:
    """
    限制Django Admin只能从特定IP访问
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 检查是否是admin路径
        if request.path.startswith('/secret-admin-panel-2024/'):
            # 获取客户端IP
            client_ip = self.get_client_ip(request)
            
            # 允许的IP列表（从settings获取）
            allowed_ips = getattr(settings, 'ADMIN_ALLOWED_IPS', [])
            
            # 如果没有配置允许的IP，则允许所有IP（开发环境）
            if not allowed_ips:
                return self.get_response(request)
            
            # 检查IP是否在允许列表中
            if not self.is_ip_allowed(client_ip, allowed_ips):
                return HttpResponseForbidden(
                    '<h1>403 Forbidden</h1><p>Access denied from your IP address.</p>'
                )
        
        return self.get_response(request)
    
    def get_client_ip(self, request):
        """获取客户端真实IP"""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
    
    def is_ip_allowed(self, client_ip, allowed_ips):
        """检查IP是否在允许列表中"""
        try:
            client_ip_obj = ipaddress.ip_address(client_ip)
            for allowed_ip in allowed_ips:
                if '/' in allowed_ip:  # CIDR notation
                    if client_ip_obj in ipaddress.ip_network(allowed_ip, strict=False):
                        return True
                else:  # Single IP
                    if client_ip_obj == ipaddress.ip_address(allowed_ip):
                        return True
        except (ipaddress.AddressValueError, ValueError):
            return False
        return False


class DevelopmentDatabaseRecoveryMiddleware:
    """
    In development, recover from a reset/missing Postgres database by
    recreating it and applying migrations once, then retry the request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except OperationalError as exc:
            if not should_attempt_runtime_db_bootstrap() or not is_missing_database_error(exc):
                raise
            ok, detail = ensure_runtime_database_ready()
            if ok:
                try:
                    return self.get_response(request)
                except OperationalError:
                    logger.exception("[db-bootstrap] request retry failed path=%s", request.path)
            else:
                logger.error("[db-bootstrap] unable to recover database for path=%s detail=%s", request.path, detail)
            return self._build_safe_response(request)

    @staticmethod
    def _build_safe_response(request):
        message = "服务暂时不可用，请稍后重试"
        if request.path.startswith("/api/"):
            return JsonResponse({"result": 0, "message": message, "code": 503}, status=503)
        return HttpResponseServerError(message)
