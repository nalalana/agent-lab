"""
/backend/apps/user/services.py
用户服务类
"""
import time
import uuid
import jwt
import logging
import secrets
import requests
import datetime
import hashlib
import hmac
import json
from django.conf import settings
from django.db import transaction
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone

from apps.user.models import User
from apps.user.permissions import get_platform_administrator
from common.utils.Sending_utils import send_email, sign_token, check_token, create_code
from django.core.cache import cache
from common.utils.Login_utils import hash_encode, verify_hash
from common.utils.Tengcentcos_utils import get_avatar_presigned_url, normalize_cos_key
from apps.user.sms_providers import get_sms_provider
from apps.risk_control.services import RiskControlService
from apps.moderation.services import ImageModerationService
try:
    from properties import db_production_base_url as production_base_url, db_local_base_url as local_base_url
except ImportError:
    production_base_url = getattr(settings, "PRODUCTION_BASE_URL", "http://127.0.0.1:8000")
    local_base_url = getattr(settings, "LOCAL_BASE_URL", "http://127.0.0.1:8000")

logger = logging.getLogger('touming')

TERMS_VERSION = "v0.3.0"
PRIVACY_VERSION = "v0.3.0"


def _ip_suffix(client_ip: str | None) -> str:
    return f" ip={client_ip}" if client_ip else ""


class UserService:
    """用户服务类"""

    @staticmethod
    def create_or_activate_user_by_phone(uid: str, username: str, phone: str, password: str | None = None, client_ip: str | None = None, agreed_to_terms: bool = False):
        """通过手机号创建或激活用户。若用户已存在未激活则激活，已激活则返回；否则创建新用户。
        """
        try:
            user = User.objects.filter(phone=phone).first()
            if user:
                # 更新用户名（如需要）
                if username and user.username != username:
                    user.username = username
                if password:
                    user.user_password = hash_encode(password)
                user.updated_at = str(time.time())[:20]
                user.save()
                return user
            # 新建
            if not agreed_to_terms:
                raise ValidationError("请先阅读并同意用户协议和隐私政策")
            user = User.objects.create(
                uid=uid,
                username=username or phone,
                phone=phone,
                user_password=hash_encode(password) if password else hash_encode(create_code(10)),
                email=None,
                created_at=str(time.time())[:20],
                updated_at=str(time.time())[:20],
                role='user',
                terms_version=TERMS_VERSION,
                privacy_version=PRIVACY_VERSION,
                consented_at=timezone.now(),
                consent_ip=client_ip,
            )
            return user
        except Exception as e:
            logger.error(f"手机号注册/激活失败:{_ip_suffix(client_ip)} err={e}")
            raise ValidationError("用户创建失败")
    
    @staticmethod
    def activate_user(user_id, email=None, password=None, client_ip: str | None = None):
        """激活用户"""
        try:
            user = User.objects.get(id=user_id)
            
            # 检查是否重复激活
            if User.objects.filter(username=user.username).count() > 1 or \
               User.objects.filter(email=user.email).count() > 1:
                raise ValidationError("刚刚有人抢先使用了该用户名或邮箱！")
            
            if email:
                user.email = email
                user.save()
                return "注册激活成功"
            elif password:
                user.user_password = password
                user.save()
                return "密码修改成功"
            else:
                raise ValidationError("无效操作")
                
        except User.DoesNotExist:
            raise ValidationError("用户不存在")
        except Exception as e:
            logger.error(f"激活用户失败:{_ip_suffix(client_ip)} err={e}")
            raise ValidationError("激活失败")
    
    @staticmethod
    def send_activation_email(user, email, mail_type='register', client_ip: str | None = None):
        """发送激活邮件"""
        try:
            payload = {
                'user_id': user.id,
                'email': email,
            }
            
            send_result = send_email(payload, email, mail_type)
            if not send_result:
                logger.error(f"发送激活邮件失败!请检查邮箱格式: {email}{_ip_suffix(client_ip)}")
                return False
            else:
                logger.info(f"发送激活邮件成功!请及时在邮箱中查收: {email}{_ip_suffix(client_ip)}")
                return True
        except Exception as e:
            logger.error(f"发送邮件失败:{_ip_suffix(client_ip)} err={e}")
            return False
    
    @staticmethod
    def authenticate_user(username, password, client_ip: str | None = None):
        """用户认证"""
        try:
            user = User.objects.get(username=username)
            if verify_hash(password, user.user_password):
                return user
            return None
        except User.DoesNotExist:
            return None

    @staticmethod
    def authenticate_by_phone_code(phone: str, code: str, client_ip: str | None = None, agreed_to_terms: bool = False):
        """通过短信验证码认证，若用户不存在则创建并激活。"""
        try:
            if not SMSService.verify_code(phone, code):
                return None
            user = User.objects.filter(phone=phone).first()
            if user:
                return user
            # 不存在则创建激活
            uid = str(uuid.uuid4()).replace('-', '')
            return UserService.create_or_activate_user_by_phone(uid=uid, username="user_" + uid[:6], phone=phone, password=None, client_ip=client_ip, agreed_to_terms=agreed_to_terms)
        except Exception as e:
            logger.error(f"短信登录失败:{_ip_suffix(client_ip)} err={e}")
            return None

    @staticmethod
    def authenticate_by_phone_password(phone: str, password: str, client_ip: str | None = None):
        """手机号+密码认证"""
        try:
            user = User.objects.get(phone=phone)
            if verify_hash(password, user.user_password):
                return user
            return None
        except User.DoesNotExist:
            return None
        except Exception as e:
            logger.error(f"手机号密码登录失败:{_ip_suffix(client_ip)} err={e}")
            return None
    
    @staticmethod
    def update_user_profile(user_id, **kwargs):
        """更新用户资料"""
        try:
            user = User.objects.get(id=user_id)
            
            # 更新允许的字段；avatar 若为完整 presigned URL 则规范为只存 COS key
            allowed_fields = ['username', 'email', 'avatar', 'bio']
            for field, value in kwargs.items():
                if field in allowed_fields and value is not None:
                    if field == 'avatar' and value:
                        ok, normalized_key, message = ImageModerationService.require_approved_public_image(value)
                        if not ok:
                            raise ValidationError(message)
                        value = normalized_key
                    setattr(user, field, value)
            
            user.updated_at = str(time.time())[:20]
            user.save()
            return user
        except User.DoesNotExist:
            raise ValidationError("用户不存在")
        except ValidationError:
            raise
        except Exception as e:
            client_ip = kwargs.get('client_ip')
            logger.error(f"更新用户资料失败:{_ip_suffix(client_ip)} err={e}")
            raise ValidationError("更新失败")
    

    @staticmethod
    def change_password_by_phone_code(user: User, new_password: str, client_ip: str | None = None):
        """通过手机号+验证码修改密码（不要求旧密码）"""
        try:

            user.user_password = hash_encode(new_password)
            user.updated_at = str(time.time())[:20]
            user.save()
            return True
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"手机号验证码改密失败:{_ip_suffix(client_ip)} err={e}")
            raise ValidationError("修改密码失败")
    
    @staticmethod
    def search_users(user_name='', limit=20, offset=0, client_ip: str | None = None):
        """搜索用户"""
        try:
            # 根据用户名模糊搜索
            queryset = User.objects.filter(username__icontains=user_name)
            total = queryset.count()
            users = list(queryset.order_by('id')[offset: offset + limit])
            
            # 计算是否有更多数据
            has_more = (offset + limit) < total
            
            user_list = []
            for user in users:
                user_list.append(user.get_info())
            
            return {
                'users': user_list,
                'total': total,
                'hasMore': has_more
            }
        except Exception as e:
            logger.error(f"搜索用户失败:{_ip_suffix(client_ip)} err={e}")
            return {
                'users': [],
                'total': 0,
                'hasMore': False
            }
    
    @staticmethod
    def get_user_by_uid(uid, client_ip: str | None = None):
        """根据 uid 获取用户信息"""
        try:
            user = User.objects.get(uid=uid)
            return {
                'uid': getattr(user, 'uid', None),
                'role': getattr(user, 'role', None),
                'phone': getattr(user, 'phone', None),
                'username': getattr(user, 'username', None),
                'description': getattr(user, 'bio', None),
                'avatar_url': get_avatar_presigned_url(getattr(user, 'avatar', None)),
                'followers': getattr(user, 'followers_count', 0),
                'following_user_count': getattr(user, 'following_user_count', 0),
                'following_opengate_count': getattr(user, 'following_opengate_count', 0),
                'following_company_count': getattr(user, 'following_company_count', 0),
                'email': getattr(user, 'email', None),
                'created_at': getattr(user, 'created_at', None),
                'updated_at': getattr(user, 'updated_at', None),
            }
        except User.DoesNotExist:
            return None
        except Exception as e:
            logger.error(f"获取用户信息失败:{_ip_suffix(client_ip)} err={e}")
            return None
    
    @staticmethod
    def upload_avatar(user_id, avatar_url, client_ip: str | None = None, require_approved: bool = True):
        """上传头像。支持传入 COS file_key 或完整 presigned URL，存库时统一为 key。"""
        try:
            user = User.objects.get(id=user_id)
            if require_approved:
                ok, normalized_key, message = ImageModerationService.require_approved_public_image(avatar_url)
                if not ok:
                    raise ValidationError(message)
            else:
                normalized_key = normalize_cos_key(avatar_url) if avatar_url else ''
            user.avatar = normalized_key
            user.updated_at = str(time.time())[:20]
            user.save()
            return True
        except User.DoesNotExist:
            raise ValidationError("用户不存在")
        except Exception as e:
            logger.error(f"上传头像失败:{_ip_suffix(client_ip)} err={e}")
            raise ValidationError("上传头像失败")
    
    @staticmethod
    @transaction.atomic
    def _change_platform_admin_role(current_user_id, target_username, *, enabled, client_ip=None):
        action = "添加" if enabled else "删除"
        try:
            actor = get_platform_administrator(current_user_id, lock=True)
            if actor is None:
                return False, "用户不是平台管理员"
            target = User.objects.select_for_update().get(username=target_username)
            if target.role == "root":
                return False, "不能修改超级管理员角色"
            target.role = "admin" if enabled else "user"
            target.updated_at = str(time.time())[:20]
            target.save(update_fields=("role", "updated_at"))
            logger.info(
                "platform_admin_role_changed actor=%s target=%s role=%s%s",
                actor.uid, target.uid, target.role, _ip_suffix(client_ip),
            )
            return True, f"{action}成功"
        except User.DoesNotExist:
            return False, "目标用户不存在"
        except Exception as exc:
            logger.error(f"{action}管理员失败:{_ip_suffix(client_ip)} err={exc}")
            return False, f"{action}失败"

    @staticmethod
    def add_admin(current_user_id, target_username, client_ip: str | None = None):
        """Grant the shared platform role for every administration module."""
        return UserService._change_platform_admin_role(
            current_user_id, target_username, enabled=True, client_ip=client_ip,
        )

    @staticmethod
    def remove_admin(current_user_id, target_username, client_ip: str | None = None):
        """Remove the shared role; authorization rechecks the role on every request."""
        return UserService._change_platform_admin_role(
            current_user_id, target_username, enabled=False, client_ip=client_ip,
        )

    @staticmethod
    def init_root_user(username='xudawei', client_ip: str | None = None):
        """初始化root用户"""
        try:
            user = User.objects.get(username=username)
            user.role = 'root'
            user.save()
            return True, "初始化成功"
        except User.DoesNotExist:
            return False, "用户不存在"
        except Exception as e:
            logger.error(f"初始化root用户失败:{_ip_suffix(client_ip)} err={e}")
            return False, "初始化失败"

    @staticmethod
    def search_users_by_phone(phone: str, limit=20, offset=0, client_ip: str | None = None):
        """根据手机号搜索用户"""
        try:
            queryset = User.objects.filter(phone__icontains=phone).order_by('id')
            total = queryset.count()
            users = list(queryset[offset: offset + limit])
            return {
                'result': 1,
                'message': 'success',
                'data': {
                    'users': [user.get_info() for user in users],
                    'total': total,
                    'hasMore': (offset + limit) < total
                }
            }
        except Exception as e:
            logger.error(f"根据手机号搜索用户失败:{_ip_suffix(client_ip)} err={e}")
            return {
                'result': 0,
                'message': '搜索失败'
            }


class CaptchaService:
    """验证码服务类"""
    
    @staticmethod
    def generate_captcha():
        """生成验证码"""
        from captcha.models import CaptchaStore
        from captcha.helpers import captcha_image_url
        
        try:
            hashkey = CaptchaStore.generate_key()
            image_url = captcha_image_url(hashkey)
            return {
                'success': True,
                'data': {
                    'image_url': image_url,
                    'key': hashkey
                }
            }
        except Exception as e:
            logger.error(f"生成验证码失败: {e}")
            return {
                'success': False,
                'message': '生成验证码失败'
            }
    
    @staticmethod
    def verify_captcha(captcha_key, user_input):
        """验证验证码"""
        from captcha.models import CaptchaStore
        
        try:
            cs = CaptchaStore.objects.get(hashkey=captcha_key)
            return cs.response == user_input.lower()
        except CaptchaStore.DoesNotExist:
            return False
        except Exception as e:
            logger.error(f"验证验证码失败: {e}")
            return False
    


class TencentCaptchaService:
    """Tencent Cloud captcha verification and one-time captcha token service."""

    TOKEN_CACHE_KEY = "tencent_captcha_token:{token}"
    DEFAULT_SCENE = "login_sms"
    ALLOWED_SCENES = {"login_sms", "register_sms", "profile_sms", "password_login"}

    @staticmethod
    def _signed_headers(endpoint: str, payload: str) -> dict:
        secret_id = str(getattr(settings, "TENCENTCLOUD_SECRET_ID", "") or "").strip()
        secret_key = str(getattr(settings, "TENCENTCLOUD_SECRET_KEY", "") or "").strip()
        headers = {
            "Content-Type": "application/json",
            "Host": endpoint,
            "X-TC-Action": "DescribeCaptchaResult",
            "X-TC-Version": "2019-07-22",
            "X-TC-Region": str(getattr(settings, "TENCENT_CAPTCHA_REGION", "ap-guangzhou") or "ap-guangzhou"),
        }
        if not secret_id or not secret_key:
            logger.warning("Tencent Cloud SecretId/SecretKey not configured; sending unsigned captcha request")
            return headers

        timestamp = int(time.time())
        date = datetime.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        service = "captcha"
        algorithm = "TC3-HMAC-SHA256"
        hashed_request_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                f"content-type:application/json\nhost:{endpoint}\n",
                "content-type;host",
                hashed_request_payload,
            ]
        )
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = "\n".join(
            [
                algorithm,
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        secret_date = hmac.new(("TC3" + secret_key).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
        secret_service = hmac.new(secret_date, service.encode("utf-8"), hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders=content-type;host, Signature={signature}"
        )
        headers["X-TC-Timestamp"] = str(timestamp)
        return headers

    @staticmethod
    def verify_ticket(ticket: str, randstr: str, scene: str, client_ip: str | None = None) -> tuple[bool, dict | str]:
        app_id = str(getattr(settings, "TENCENT_CAPTCHA_APP_ID", "") or "").strip()
        secret_key = str(getattr(settings, "TENCENT_CAPTCHA_SECRET_KEY", "") or "").strip()
        endpoint = str(getattr(settings, "TENCENT_CAPTCHA_ENDPOINT", "captcha.tencentcloudapi.com") or "").strip()
        ttl = int(getattr(settings, "TENCENT_CAPTCHA_TOKEN_TTL", 300) or 300)
        scene = scene if scene in TencentCaptchaService.ALLOWED_SCENES else TencentCaptchaService.DEFAULT_SCENE

        if not ticket or not randstr:
            return False, "验证码参数不完整"
        if not app_id or not secret_key:
            logger.error("Tencent captcha config missing")
            return False, "验证码服务暂不可用"

        payload = {
            "CaptchaType": 9,
            "Ticket": ticket,
            "Randstr": randstr,
            "UserIp": client_ip or "",
            "CaptchaAppId": int(app_id) if app_id.isdigit() else app_id,
            "AppSecretKey": secret_key,
            "NeedGetCaptchaTime": 1,
        }
        request_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = TencentCaptchaService._signed_headers(endpoint, request_body)

        try:
            response = requests.post(
                f"https://{endpoint}",
                data=request_body.encode("utf-8"),
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
            body = response.json()
            result = body.get("Response", body)
            captcha_code = int(result.get("CaptchaCode", 0) or 0)
            if captcha_code != 1:
                logger.warning(
                    f"Tencent captcha rejected code={captcha_code} scene={scene}{_ip_suffix(client_ip)}"
                )
                return False, "验证码校验失败"

            token = secrets.token_urlsafe(32)
            cache.set(
                TencentCaptchaService.TOKEN_CACHE_KEY.format(token=token),
                {"scene": scene, "ip": client_ip or "", "phone": None},
                ttl,
            )
            return True, {"captcha_token": token, "expires_in": ttl}
        except requests.RequestException as e:
            logger.error(f"Tencent captcha request failed scene={scene}{_ip_suffix(client_ip)} err={e}")
            return False, "验证码服务暂不可用"
        except Exception as e:
            logger.error(f"Tencent captcha verify failed scene={scene}{_ip_suffix(client_ip)} err={e}")
            return False, "验证码校验失败"

    @staticmethod
    def consume_token(token: str, scene: str, phone: str, client_ip: str | None = None) -> tuple[bool, str]:
        if not token:
            return False, "请先完成安全验证"
        scene = scene if scene in TencentCaptchaService.ALLOWED_SCENES else TencentCaptchaService.DEFAULT_SCENE
        cache_key = TencentCaptchaService.TOKEN_CACHE_KEY.format(token=token)
        data = cache.get(cache_key)
        if not data:
            return False, "验证码已失效，请重新验证"
        if data.get("scene") != scene:
            return False, "验证码场景不匹配，请重新验证"
        if data.get("ip") and data.get("ip") != (client_ip or ""):
            return False, "验证码已失效，请重新验证"

        cache.delete(cache_key)
        return True, "success"


class SMSService:
    """短信验证码服务。请求频率、封禁和失败状态统一交给 risk_control app 管理。"""

    CACHE_KEY = "sms_code:{phone}"

    @staticmethod
    def _hash_value(value: str | None) -> str:
        return RiskControlService.hash_value(value)[:32]

    @staticmethod
    def _mask_phone(phone: str) -> str:
        if len(phone) <= 7:
            return f"{phone[:2]}***{phone[-2:]}"
        return f"{phone[:3]}****{phone[-4:]}"

    @staticmethod
    def _mask_ip(client_ip: str | None) -> str:
        if not client_ip:
            return "unknown"
        parts = client_ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:2] + ["*", "*"])
        if ":" in client_ip:
            chunks = client_ip.split(":")
            return ":".join(chunks[:2] + ["*"])
        return "***"

    @staticmethod
    def send_code(
        phone: str,
        ttl_seconds: int = 300,
        client_ip: str | None = None,
        captcha_token: str = "",
        scene: str = "login_sms",
    ) -> tuple[bool, str, bool]:
        """
        发送短信验证码
        返回: (是否成功, 错误消息, 是否限流)
        """
        try:
            captcha_ok, captcha_message = TencentCaptchaService.consume_token(
                captcha_token,
                scene,
                phone,
                client_ip=client_ip,
            )
            if not captcha_ok:
                return False, captcha_message, False

            risk_decision = RiskControlService.evaluate(
                "sms.send",
                phone=phone,
                ip=client_ip,
                scene=scene,
            )
            if not risk_decision.allowed:
                logger.warning(
                    f"短信发送风控拦截 phone={SMSService._mask_phone(phone)} "
                    f"ip={SMSService._mask_ip(client_ip)} rule={risk_decision.matched_rule}"
                )
                return False, risk_decision.reason, True

            # Tencent SMS verification-code templates require a numeric code.
            code = f"{secrets.randbelow(1_000_000):06d}"
            provider_result = get_sms_provider().send_code(phone, str(code), ttl_seconds)
            if not provider_result.success:
                return False, provider_result.message or "发送短信失败，请稍后重试", False

            # 存储验证码
            cache.set(SMSService.CACHE_KEY.format(phone=phone), code, ttl_seconds)
            
            logger.info(
                f"短信验证码已发送 phone={SMSService._mask_phone(phone)} "
                f"ip={SMSService._mask_ip(client_ip)}"
            )
            return True, "验证码发送成功", False
            
        except Exception as e:
            logger.error(f"发送短信失败:{_ip_suffix(client_ip)} err={e}")
            return False, "发送短信失败，请稍后重试", False

    @staticmethod
    def verify_code(phone: str, code: str) -> bool:
        cache_key = SMSService.CACHE_KEY.format(phone=phone)
        cached = cache.get(cache_key)
        if cached is not None and str(cached) == str(code):
            cache.delete(cache_key)
            RiskControlService.reset_failure("sms.verify_failure", phone=phone)
            return True
        decision = RiskControlService.record_failure("sms.verify_failure", phone=phone)
        if not decision.allowed:
            logger.warning(
                f"短信验证码错误次数过多 phone={SMSService._mask_phone(phone)} "
                f"rule={decision.matched_rule}"
            )
        return False


class TokenService:
    """Token服务类"""
    
    @staticmethod
    def generate_token(payload, type='access', expires_in: int = 3600 * 24 * 2, client_ip: str | None = None):
        """生成Token"""
        try:
            return sign_token(payload, exp=expires_in)
        except Exception as e:
            logger.error(f"生成Token失败:{_ip_suffix(client_ip)} type={type} expires_in={expires_in} err={e}")
            return None
    
    @staticmethod
    def verify_token(token, client_ip: str | None = None):
        """验证Token"""
        try:
            return check_token(token)
        except Exception as e:
            logger.error(f"验证Token失败:{_ip_suffix(client_ip)} err={e}")
            return None
    
    @staticmethod
    def get_email_url(token, client_ip: str | None = None):
        """获取邮件链接URL"""
        try:
            base_url = getattr(settings, 'BASE_URL', None)
            if base_url:
                return base_url + "/api/user/email/" + token
            elif settings.DEBUG:
                return local_base_url + "/api/user/email/" + token
            else:
                return production_base_url + "/api/user/email/" + token
        except Exception as e:
            logger.error(f"获取邮件URL失败:{_ip_suffix(client_ip)} err={e}")
            return None
    
    @staticmethod
    def refresh_token(token, client_ip: str | None = None):
        """刷新Token"""
        try:
            import jwt
            from common.utils.Login_utils import sign_token
            
            # 尝试正常解析token
            try:
                payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
            except Exception as e:
                logger.error(f"解析token失败:{_ip_suffix(client_ip)} err={e}")
                return False, None
            
            # 生成新的token
            access_token = sign_token(payload, exp=3600 * 24 * 2)
            refresh_token = sign_token(payload, exp=3600 * 24 * 30)
            return True, {
                'access_token': access_token,
                'refresh_token': refresh_token
            }
            
        except Exception as e:
            logger.error(f"刷新Token失败:{_ip_suffix(client_ip)} err={e}")
            return False, None
