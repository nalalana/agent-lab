"""
鐧诲綍妫€娴嬬浉鍏冲伐鍏风被锛堜粠 /backend/utils/Login_utils.py 杩佺Щ鍒?/backend/common/utils/Login_utils.py锛?
"""
import hashlib
import ipaddress
import os
import time
import uuid
import jwt
import json
from django.http import JsonResponse
from django.conf import settings
from django.db.models import F
from apps.opengate.models import APIKey
from apps.risk_control.services import RiskControlService
import logging
import hmac

logger = logging.getLogger('touming')
API_KEY_TOKEN_PREFIX = "tmk_"


def generate_signature(secret_key, payload):
    return hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def generate_api_key_pair():
    secret_id = "skid_" + str(uuid.uuid4()).replace('-', '')[:16]
    secret_key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    secret_verification = hashlib.sha256(secret_key.encode() + secret_id.encode()).hexdigest()
    return secret_id, secret_key, secret_verification


def make_api_bearer_token(secret_id, secret_key):
    return f"{API_KEY_TOKEN_PREFIX}{secret_id}.{secret_key}"


def parse_api_bearer_token(token):
    if not token or not token.startswith(API_KEY_TOKEN_PREFIX):
        return None, None
    raw_token = token[len(API_KEY_TOKEN_PREFIX):]
    if "." not in raw_token:
        return None, None
    secret_id, secret_key = raw_token.split(".", 1)
    if not secret_id.startswith("skid_") or not secret_key:
        return None, None
    return secret_id, secret_key


def verify_api_key_pair(secret_id, secret_key, secret_verification):
    digest = hashlib.sha256(secret_key.encode() + secret_id.encode()).hexdigest()
    return hmac.compare_digest(digest, secret_verification)


# Hash-md5 鍔犲瘑瀛楃涓?
TOKEN_SECRET_KEY = settings.SECRET_KEY


def hash_encode(str_key: str, salt: bytes = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    md5 = hashlib.md5()
    md5.update(salt + str_key.encode())
    return f"{salt.hex()}${md5.hexdigest()}"


def verify_hash(str_key: str, stored_hash: str) -> bool:
    salt_hex, hash_value = stored_hash.split("$")
    salt = bytes.fromhex(salt_hex)
    return hash_encode(str_key, salt) == stored_hash


def sign_token(payload, exp=3600 * 24 * 2):
    payload['exp'] = time.time() + exp
    token = jwt.encode(payload, TOKEN_SECRET_KEY, algorithm='HS256')
    return token


def check_token(token):
    try:
        payload = jwt.decode(token, TOKEN_SECRET_KEY, algorithms=['HS256'])
    except Exception:
        return None
    return payload


def login_checker(func):
    def wrap(request, *args, **kwargs):
        token = request.META.get('HTTP_ACCESS_TOKEN')

        if not token:
            auth_header = request.META.get('HTTP_AUTHORIZATION', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:].strip()

        # 鍏煎閫氳繃 URL query 浼犻€?token 鐨勫満鏅紝渚嬪锛?
        # /api/sso/authorize?...&access_token=xxx
        if not token:
            token = request.GET.get('access_token') or request.GET.get('token')

        payload = check_token(token)
        if payload is None or payload['user_id'] <= 0:
            result = {'result': 0, 'message': '请先登录', 'code': 401}
            return JsonResponse(result, status=401)
        request.user_id = payload.get('user_id', 0)
        return func(request, *args, **kwargs)
    return wrap


def api_key_checker(func):
    def wrap(request, *args, **kwargs):
        secret_id = None
        secret_key = None
        try:
            auth_header = request.META.get('HTTP_AUTHORIZATION', '')
            if auth_header.startswith('Bearer '):
                secret_id, secret_key = parse_api_bearer_token(auth_header[7:].strip())

            if not secret_id or not secret_key:
                try:
                    data_json = json.loads(request.body.decode() or '{}')
                except (json.JSONDecodeError, UnicodeDecodeError):
                    data_json = {}
                secret_id = data_json.get('secret_id', None)
                secret_key = data_json.get('secret_key', None)

            if not secret_id or not secret_key:
                result = {'result': 0, 'message': 'API key is required', 'code': 401}
                return JsonResponse(result, status=401)

            verify_decision = RiskControlService.evaluate(
                "api_key.verify",
                request,
                api_key_id=secret_id,
            )
            if not verify_decision.allowed:
                return JsonResponse(verify_decision.to_response_payload(), status=verify_decision.status_code)

            key_obj = APIKey.objects.get(secret_id=secret_id, is_active=APIKey.IS_ACTIVE_ACTIVE)
            if not verify_api_key_pair(secret_id, secret_key, key_obj.secret_verification):
                failure_decision = RiskControlService.record_failure(
                    "api_key.verify",
                    request,
                    api_key_id=secret_id,
                )
                if not failure_decision.allowed:
                    return JsonResponse(failure_decision.to_response_payload(), status=failure_decision.status_code)
                result = {'result': 0, 'message': 'API key verification failed', 'code': 401}
                return JsonResponse(result, status=401)

            risk_decision = RiskControlService.evaluate(
                "api_key.call",
                request,
                api_key_id=secret_id,
                actor_user_id=getattr(key_obj.user, "id", "") if key_obj.user else "",
                company_id=getattr(key_obj.gate, "gid", ""),
                resource_id=getattr(key_obj.gate, "gid", ""),
            )
            if not risk_decision.allowed:
                return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

            APIKey.objects.filter(pk=key_obj.pk).update(
                last_used_at=str(time.time())[:20],
                last_used_ip=get_client_ip(request),
                last_used_path=request.path[:255],
                usage_count=F('usage_count') + 1,
            )
            key_obj.refresh_from_db(fields=['last_used_at', 'last_used_ip', 'last_used_path', 'usage_count'])
        except APIKey.DoesNotExist:
            failure_decision = RiskControlService.record_failure(
                "api_key.verify",
                request,
                api_key_id=secret_id,
            )
            if not failure_decision.allowed:
                return JsonResponse(failure_decision.to_response_payload(), status=failure_decision.status_code)
            result = {'result': 0, 'message': 'API key is invalid or inactive', 'code': 401}
            return JsonResponse(result, status=401)
        except Exception:
            logger.exception("[api_key_checker] API key verification failed")
            result = {'result': 0, 'message': 'API key verification failed', 'code': 401}
            return JsonResponse(result, status=401)
        request.gid = key_obj.gate.gid
        request.api_key = key_obj
        request.api_key_user = key_obj.user
        return func(request, *args, **kwargs)
    return wrap


def _is_trusted_proxy(remote_addr: str | None) -> bool:
    if not remote_addr:
        return False

    trusted_proxies = getattr(settings, "TRUSTED_PROXY_IPS", [])
    if isinstance(trusted_proxies, str):
        trusted_proxies = [item.strip() for item in trusted_proxies.split(",") if item.strip()]

    try:
        remote_ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False

    for proxy in trusted_proxies:
        try:
            if "/" in proxy:
                if remote_ip in ipaddress.ip_network(proxy, strict=False):
                    return True
            elif remote_ip == ipaddress.ip_address(proxy):
                return True
        except ValueError:
            logger.warning(f"[get_client_ip] invalid TRUSTED_PROXY_IPS entry={proxy}")

    return remote_ip.is_loopback


def _first_valid_forwarded_ip(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    for candidate in raw_value.split(","):
        ip = candidate.strip()
        try:
            ipaddress.ip_address(ip)
            return ip
        except ValueError:
            continue
    return None


def get_client_ip(request):
    """获取客户端 IP。仅在请求来自可信代理时使用转发头，避免客户端伪造 X-Forwarded-For。"""
    remote_addr = request.META.get("REMOTE_ADDR")
    if _is_trusted_proxy(remote_addr):
        forwarded_ip = _first_valid_forwarded_ip(request.META.get("HTTP_X_FORWARDED_FOR"))
        if forwarded_ip:
            return forwarded_ip
        real_ip = _first_valid_forwarded_ip(request.META.get("HTTP_X_REAL_IP"))
        if real_ip:
            return real_ip
    return remote_addr


