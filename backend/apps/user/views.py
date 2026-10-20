"""
/backend/apps/user/views.py
用户视图函数
"""
import json
import logging
import os
import uuid
from django.http import JsonResponse
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from apps.user.models import User
from apps.user.serializers import (
    UserLoginSerializer, UserProfileSerializer,
    UserUpdateSerializer,  
    TokenRefreshSerializer, TwoStepVerifySerializer, UserSearchSerializer,
    UserViewSerializer, AvatarUploadSerializer, AdminAddSerializer, AdminDeleteSerializer,
    PhonePasswordChangeSerializer
)
from apps.user.services import (
    UserService, CaptchaService, TokenService, SMSService, TencentCaptchaService
)
from apps.iam.services import get_or_create_principal
from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService, moderation_payload
from apps.risk_control.services import RiskControlService
from common.utils.Login_utils import login_checker, get_client_ip
from common.utils.common_utils import get_user_from_request
from common.utils.Tengcentcos_utils import put_object, get_avatar_presigned_url

logger = logging.getLogger('touming')


def _first_error_message(errors, default="请求参数不正确"):
    if isinstance(errors, (list, tuple)):
        for item in errors:
            message = _first_error_message(item, "")
            if message:
                return message
        return default
    if isinstance(errors, dict):
        preferred_keys = ("non_field_errors", "phone", "password", "password1", "password2", "code", "username")
        for key in preferred_keys:
            if key in errors:
                message = _first_error_message(errors[key], "")
                if message:
                    return message
        for value in errors.values():
            message = _first_error_message(value, "")
            if message:
                return message
        return default
    if errors:
        return str(errors)
    return default


@api_view(['GET'])
def get_captcha(request):
    """获取验证码"""
    result = CaptchaService.generate_captcha()
    
    if result['success']:
        return JsonResponse({
            'result': 1, 
            'message': '获取验证码成功', 
            'data': result['data']
        })
    else:
        return JsonResponse({
            'result': 0, 
            'message': result['message']
        }, status=400)


@api_view(['POST'])
def verify_tencent_captcha(request):
    """Verify Tencent captcha ticket and issue one-time captcha token."""
    try:
        data = json.loads(request.body.decode() or "{}")
        from apps.user.serializers import TencentCaptchaVerifySerializer

        serializer = TencentCaptchaVerifySerializer(data=data)
        if not serializer.is_valid():
            return JsonResponse({"result": 0, "message": _first_error_message(serializer.errors)}, status=400)

        ok, result = TencentCaptchaService.verify_ticket(
            ticket=serializer.validated_data["ticket"],
            randstr=serializer.validated_data["randstr"],
            scene=serializer.validated_data["scene"],
            client_ip=get_client_ip(request),
        )
        if not ok:
            return JsonResponse({"result": 0, "message": result}, status=400)
        return JsonResponse({"result": 1, "message": "success", "data": result})
    except Exception as e:
        logger.error(f"Tencent captcha verify view failed ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({"result": 0, "message": "验证码校验失败"}, status=500)


@api_view(['GET'])
def active(request, token):
    """用户激活"""
    payload = TokenService.verify_token(token)
    
    # 构建邮件内容URL
    if settings.DEBUG:
        content_url = settings.LOCAL_BASE_URL + '/auth/login'
    else:
        content_url = settings.PRODUCTION_BASE_URL
    
    content = {'url': content_url}
    
    if payload is None:
        content["title"] = "操作失败"
        content["message"] = "链接失效啦！"
        return render(request, 'EmailContent-check.html', content)
    
    user_id = payload.get('user_id')
    
    try:
        if 'email' in payload:
            message = UserService.activate_user(user_id, email=payload.get('email'), client_ip=get_client_ip(request))
            content["title"] = "感谢注册"
            content["message"] = message
        elif 'password' in payload:
            message = UserService.activate_user(user_id, password=payload.get('password'), client_ip=get_client_ip(request))
            content["title"] = "修改成功"
            content["message"] = message
        else:
            content["title"] = "操作失败"
            content["message"] = "无效操作"
            
        return render(request, 'EmailContent-check.html', content)
        
    except Exception as e:
        content["title"] = "激活失败"
        content["message"] = str(e)
        return render(request, 'EmailContent-check.html', content)


@api_view(['POST'])
def login_by_password(request):
    """用户登录"""
    try:
        data = json.loads(request.body.decode())
        serializer = UserLoginSerializer(data=data)
        
        if serializer.is_valid():
            phone = serializer.validated_data['phone']
            password = serializer.validated_data['password']
            risk_decision = RiskControlService.evaluate(
                "auth.password_login",
                request,
                phone=phone,
            )
            if not risk_decision.allowed:
                return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

            captcha_ok, captcha_message = TencentCaptchaService.consume_token(
                data.get('captcha_token', ''),
                'password_login',
                phone,
                client_ip=get_client_ip(request),
            )
            if not captcha_ok:
                return JsonResponse({
                    'result': 0,
                    'message': captcha_message
                }, status=400)
            
            # 验证用户
            user = UserService.authenticate_by_phone_password(phone, password, client_ip=get_client_ip(request))
            
            if user:
                RiskControlService.reset_failure("auth.password_login", phone=phone, ip=get_client_ip(request))
                # 生成token（这里可以集成JWT或其他认证方式）
                token = TokenService.generate_token(payload={'user_id': user.id})
                
                # 发送登录通知（异步）
                try:
                    from apps.notification.tasks import send_login_notification_task
                    send_login_notification_task.delay(user.id, get_client_ip(request))
                except Exception as e:
                    logger.warning(f"[login] 发送登录通知失败 user_id={user.id} err={e}")
                
                return JsonResponse({
                    'result': 1,
                    'message': '登录成功',
                    'data': {
                        'token': token,
                        'user': UserProfileSerializer(user).data
                    }
                })
            else:
                failure_decision = RiskControlService.record_failure(
                    "auth.password_login",
                    request,
                    phone=phone,
                )
                if not failure_decision.allowed:
                    return JsonResponse(failure_decision.to_response_payload(), status=failure_decision.status_code)
                return JsonResponse({
                    'result': 0,
                    'message': '手机号或密码错误'
                }, status=401)
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"用户登录失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '登录失败'
        }, status=500)


@api_view(['POST'])
def send_sms_code(request):
    """发送短信验证码"""
    try:
        data = json.loads(request.body.decode())
        from apps.user.serializers import PhoneSendCodeSerializer
        serializer = PhoneSendCodeSerializer(data=data)
        if serializer.is_valid():
            phone = serializer.validated_data['phone']
            captcha_token = serializer.validated_data['captcha_token']
            scene = serializer.validated_data.get('scene') or 'login_sms'
            if scene == 'register_sms' and User.objects.filter(phone=phone).exists():
                return JsonResponse({
                    'result': 0,
                    'message': '该手机号已注册，请直接登录或使用短信验证码登录',
                }, status=400)
            ok, message, rate_limited = SMSService.send_code(
                phone,
                client_ip=get_client_ip(request),
                captcha_token=captcha_token,
                scene=scene,
            )
            if ok:
                return JsonResponse({'result': 1, 'message': message})
            return JsonResponse(
                {'result': 0, 'message': message, 'code': 429 if rate_limited else 400},
                status=429 if rate_limited else 400,
            )
        else:
            return JsonResponse({'result': 0, 'message': _first_error_message(serializer.errors)}, status=400)
    except Exception as e:
        logger.error(f"发送短信失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({'result': 0, 'message': '发送失败'}, status=500)


@api_view(['POST'])
def register_by_phone(request):
    """手机号+验证码注册/激活"""
    try:
        data = json.loads(request.body.decode())
        from apps.user.serializers import PhoneRegisterSerializer
        serializer = PhoneRegisterSerializer(data=data)
        if serializer.is_valid():
            uid = str(uuid.uuid4()).replace('-', '')
            phone = serializer.validated_data['phone']
            risk_decision = RiskControlService.evaluate(
                "auth.register",
                request,
                phone=phone,
            )
            if not risk_decision.allowed:
                return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

            code = serializer.validated_data['code']
            username = serializer.validated_data.get('username') or "user_" + uid[:6]
            password = serializer.validated_data.get('password') or None
            
            if not SMSService.verify_code(phone, code):
                return JsonResponse({'result': 0, 'message': '验证码错误'}, status=400)
            user = UserService.create_or_activate_user_by_phone(
                uid=uid,
                username=username,
                phone=phone,
                password=password,
                client_ip=get_client_ip(request),
                agreed_to_terms=serializer.validated_data['agreed_to_terms'],
            )
            access_token = TokenService.generate_token(payload={'user_id': user.id})
            refresh_token = TokenService.generate_token(payload={'user_id': user.id}, type='refresh')
            get_or_create_principal(principal_type='user', external_id=user.uid)
            return JsonResponse({'result': 1, 'message': '注册成功', 'data': {'access_token': access_token, 'refresh_token': refresh_token, 'user': UserProfileSerializer(user).data}})
        else:
            return JsonResponse({'result': 0, 'message': _first_error_message(serializer.errors)}, status=400)
    except Exception as e:
        logger.error(f"手机号注册失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({'result': 0, 'message': '注册失败'}, status=500)


@api_view(['POST'])
def login_by_phone(request):
    """手机号+验证码登录（自动注册未激活用户）"""
    try:
        data = json.loads(request.body.decode())
        from apps.user.serializers import PhoneLoginSerializer
        serializer = PhoneLoginSerializer(data=data)
        if serializer.is_valid():
            phone = serializer.validated_data['phone']
            code = serializer.validated_data['code']
            if not User.objects.filter(phone=phone).exists() and not serializer.validated_data['agreed_to_terms']:
                return JsonResponse({'result': 0, 'message': '请先阅读并同意用户协议和隐私政策'}, status=400)
            user = UserService.authenticate_by_phone_code(
                phone,
                code,
                client_ip=get_client_ip(request),
                agreed_to_terms=serializer.validated_data['agreed_to_terms'],
            )
            if not user:
                return JsonResponse({'result': 0, 'message': '验证码错误或登录失败'}, status=400)
            access_token = TokenService.generate_token(payload={'user_id': user.id})
            refresh_token = TokenService.generate_token(payload={'user_id': user.id}, type='refresh', expires_in=3600 * 24 * 30)
            
            # 发送登录通知（异步）
            try:
                from apps.notification.tasks import send_login_notification_task
                send_login_notification_task.delay(user.id, get_client_ip(request))
            except Exception as e:
                logger.warning(f"[login] 发送登录通知失败 user_id={user.id} err={e}")
            
            return JsonResponse({'result': 1, 'message': '登录成功', 'data': {'access_token': access_token, 'refresh_token': refresh_token, 'user': UserProfileSerializer(user).data}})
        else:
            return JsonResponse({'result': 0, 'message': _first_error_message(serializer.errors)}, status=400)
    except Exception as e:
        logger.error(f"手机号登录失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({'result': 0, 'message': '登录失败'}, status=500)


@api_view(['GET'])
@login_checker
def get_profile(request):
    """获取用户资料"""
    try:
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({
                'result': 0,
                'message': '请先登录'
            }, status=401)
        
        serializer = UserProfileSerializer(user)
        return JsonResponse({
            'result': 1,
            'message': '获取成功',
            'data': serializer.data
        })
    except Exception as e:
        logger.error(f"获取用户资料失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '获取失败'
        }, status=500)


@api_view(['PUT'])
@login_checker
def update_profile(request):
    """更新用户资料"""
    try:
        data = json.loads(request.body.decode())
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({
                'result': 0,
                'message': '请先登录'
            }, status=401)
        serializer = UserUpdateSerializer(
            data=data, 
            context={'user_id': user.id if user else None}
        )
        
        if serializer.is_valid():
            updated_user = UserService.update_user_profile(
                user.id,
                **serializer.validated_data,
                client_ip=get_client_ip(request)
            )
            
            return JsonResponse({
                'result': 1,
                'message': '更新成功',
                'data': UserProfileSerializer(updated_user).data
            })
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except ValidationError as e:
        msg = getattr(e, 'message', None) or (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
        return JsonResponse({'result': 0, 'message': msg}, status=400)
    except Exception as e:
        logger.error(f"更新用户资料失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '更新失败'
        }, status=500)


@api_view(['POST'])
@login_checker
def change_password(request):
    """通过手机号+验证码修改密码（不要求旧密码，不需登录）"""
    try:
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({
                'result': 0,
                'message': '请先登录'
            }, status=401)
        data = json.loads(request.body.decode())
        submitted_phone = str(data.get('phone') or '').strip()
        current_phone = str(user.phone or '').strip()
        if submitted_phone != current_phone:
            return JsonResponse({'result': 0, 'message': '手机号与当前账号不一致'}, status=400)

        serializer = PhonePasswordChangeSerializer(data=data)
        
        if serializer.is_valid():
            success = UserService.change_password_by_phone_code(
                user=user,
                new_password=serializer.validated_data['new_password1'],
                client_ip=get_client_ip(request)
            )
            
            if success:
                return JsonResponse({'result': 1, 'message': '密码修改成功'})
            return JsonResponse({'result': 0, 'message': '修改密码失败'}, status=400)
        else:
            return JsonResponse({'result': 0, 'message': _first_error_message(serializer.errors)}, status=400)
    except ValidationError as e:
        msg = getattr(e, 'message', None) or (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
        return JsonResponse({'result': 0, 'message': msg}, status=400)
    except Exception as e:
        logger.error(f"修改密码失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({'result': 0, 'message': '修改密码失败'}, status=500)


# 向后兼容的视图函数（暂时保留原有接口）
@api_view(['POST'])
def logout(request):
    """用户登出（向后兼容）"""
    return JsonResponse({'result': 1, 'message': '登出成功'})


@api_view(['POST'])
def refresh_token(request):
    """刷新Token"""
    try:
        serializer = TokenRefreshSerializer(data=request.data)
        
        if serializer.is_valid():
            token = serializer.validated_data['refresh_token']
            success, result = TokenService.refresh_token(token)
            
            if success:
                return JsonResponse({
                    'result': 1,
                    'message': 'success',
                    'data': {
                        'access_token': result['access_token'],
                        'refresh_token': result['refresh_token']
                    }
                })
            else:
                return JsonResponse({
                    'result': 0,
                    'message': result
                }, status=400)
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"刷新Token失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '刷新Token失败'
        }, status=500)

@api_view(['POST'])
@login_checker
def two_step_verify(request):
    """用户在登录状态下，进行危险操作时，进行两步验证
    1.检查用户输入的手机号和验证码是否正确
    2.检查用户输入的手机号是否和用户本人手机号相同
    3.返回一个token，过期时间设置为5分钟
    """
    try:
        serializer = TwoStepVerifySerializer(data=request.data)
        
        if serializer.is_valid():
            phone = serializer.validated_data['phone']
            code = serializer.validated_data['code']

            user = get_user_from_request(request)
            if not user:
                return JsonResponse({
                    'result': 0,
                    'message': '请先登录'
                }, status=401)
            if user.phone != phone:
                return JsonResponse({
                    'result': 0,
                    'message': '手机号不匹配'
                }, status=400)
            if not SMSService.verify_code(phone, code):
                return JsonResponse({
                    'result': 0,
                    'message': '验证码错误'
                }, status=400)
            if user.id != request.two_step_user_id:
                return JsonResponse({
                    'result': 0,
                    'message': '用户不匹配'
                }, status=400)
            return JsonResponse({
                'result': 1,
                'message': '验证成功',
                'data': {
                    'token': TokenService.generate_token(payload={'user_id': user.id}, type='two_step', expires_in=300),
                }
            })
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"两步验证失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '验证失败'
        }, status=500)



@api_view(['POST'])
def view_user(request):
    """查看用户详情"""
    try:
        data = json.loads(request.body.decode())
        serializer = UserViewSerializer(data=data)
        
        if serializer.is_valid():
            uid = serializer.validated_data['uid']
            user_info = UserService.get_user_by_uid(uid, client_ip=get_client_ip(request))
            
            if user_info:
                return JsonResponse({
                    'result': 1,
                    'message': 'success',
                    'data': user_info
                })
            else:
                return JsonResponse({
                    'result': 0,
                    'message': '用户不存在'
                }, status=404)
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"查看用户失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '查看用户失败'
        }, status=500)


@api_view(['GET'])
def search_user(request):
    """搜索用户"""
    try:
        # 从GET参数中获取数据
        user_name = request.GET.get('user_name', '')
        limit = int(request.GET.get('limit', 20))
        offset = int(request.GET.get('offset', 0))
        
        # 验证参数
        serializer = UserSearchSerializer(data={
            'user_name': user_name,
            'limit': limit,
            'offset': offset
        })
        
        if serializer.is_valid():
            result = UserService.search_users(
                user_name=serializer.validated_data['user_name'],
                limit=serializer.validated_data['limit'],
                offset=serializer.validated_data['offset'],
                client_ip=get_client_ip(request)
            )
            
            if not result['users']:
                return JsonResponse({
                    'result': 1,
                    'message': '没有找到符合条件的用户',
                    'data': [],
                    'total': 0
                })
            
            return JsonResponse({
                'result': 1,
                'message': 'success',
                'data': result['users'],
                'total': result['total']
            })
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"搜索用户失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '搜索失败'
        }, status=500)



@api_view(['POST'])
@login_checker
def upload_avatar(request):
    """上传头像（支持文件直传 COS，兼容旧的 URL 方式）"""
    try:
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({
                'result': 0,
                'message': '请先登录'
            }, status=401)

        # 新版：前端使用 multipart/form-data 上传文件字段 file
        upload_file = request.FILES.get('file') or request.FILES.get('avatar')
        if upload_file:
            # 基础校验：仅允许常见图片类型，且限制 5MB
            allowed_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
            if upload_file.content_type not in allowed_types:
                return JsonResponse({
                    'result': 0,
                    'message': '仅支持 JPG/PNG/GIF/WEBP 图片'
                }, status=400)

            max_size = 5 * 1024 * 1024
            if upload_file.size > max_size:
                return JsonResponse({
                    'result': 0,
                    'message': '头像大小不能超过 5MB'
                }, status=400)

            file_name = upload_file.name or f'avatar_{user.uid}.jpg'
            file_ext = os.path.splitext(file_name)[1].lower()
            if not file_ext:
                file_ext = '.jpg'
                file_name = f'{file_name}{file_ext}'

            file_key = put_object(upload_file.read(), file_name)
            asset = ImageModerationService.register_uploaded_image(
                file_key,
                source=ImageModerationAsset.SOURCE_USER_AVATAR,
                source_field="avatar",
                owner_type="user",
                owner_id=str(user.uid),
                mime_type=upload_file.content_type or "",
                size=upload_file.size or 0,
            )
            UserService.upload_avatar(user.id, file_key, client_ip=get_client_ip(request), require_approved=False)
            avatar_url = get_avatar_presigned_url(file_key, expires_in=3600)

            return JsonResponse({
                'result': 1,
                'message': '上传头像成功',
                'data': {
                    'avatar_url': avatar_url,
                    'file_url': avatar_url,
                    'url': avatar_url,
                    'file_key': file_key,
                    'moderation': moderation_payload(asset),
                }
            })

        # 兼容旧版：body 传 avatar URL
        data = json.loads(request.body.decode() or '{}')
        serializer = AvatarUploadSerializer(data=data)
        if not serializer.is_valid():
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)

        avatar_url = serializer.validated_data['avatar']
        UserService.upload_avatar(user.id, avatar_url, client_ip=get_client_ip(request))

        return JsonResponse({
            'result': 1,
            'message': '上传头像成功',
            'data': {
                'avatar_url': avatar_url,
                'file_url': avatar_url,
                'url': avatar_url,
            }
        })
    except ValidationError as e:
        msg = getattr(e, 'message', None) or (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
        return JsonResponse({
            'result': 0,
            'message': msg
        }, status=400)
    except Exception as e:
        logger.error(f"上传头像失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '上传头像失败'
        }, status=500)




@api_view(['POST'])
@login_checker
def admin_add(request):
    """添加管理员"""
    try:
        data = json.loads(request.body.decode())
        serializer = AdminAddSerializer(data=data)
        
        if serializer.is_valid():
            user = get_user_from_request(request)
            if not user:
                return JsonResponse({
                    'result': 0,
                    'message': '请先登录'
                }, status=401)
            
            username = serializer.validated_data['username']
            success, message = UserService.add_admin(user.id, username, client_ip=get_client_ip(request))
            
            return JsonResponse({
                'result': 1 if success else 0,
                'message': message
            })
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"添加管理员失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '添加管理员失败'
        }, status=500)


@api_view(['POST'])
@login_checker
def admin_delete(request):
    """删除管理员"""
    try:
        data = json.loads(request.body.decode())
        serializer = AdminDeleteSerializer(data=data)
        
        if serializer.is_valid():
            user = get_user_from_request(request)
            if not user:
                return JsonResponse({
                    'result': 0,
                    'message': '请先登录'
                }, status=401)
            
            username = serializer.validated_data['username']
            success, message = UserService.remove_admin(user.id, username, client_ip=get_client_ip(request))
            
            return JsonResponse({
                'result': 1 if success else 0,
                'message': message
            })
        else:
            return JsonResponse({
                'result': 0,
                'message': _first_error_message(serializer.errors)
            }, status=400)
            
    except Exception as e:
        logger.error(f"删除管理员失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '删除管理员失败'
        }, status=500)


@api_view(['POST'])
def quick_init_root(request):
    """The retired bootstrap endpoint must never change roles over public HTTP."""
    return JsonResponse(
        {
            'result': 0,
            'message': '该初始化接口已停用，请通过平台管理员授权接口分配权限',
        },
        status=410,
    )


@api_view(['GET'])
def search_users_by_phone(request):
    """根据手机号搜索用户"""
    try:
        phone = request.GET.get('phone', '')
        limit = int(request.GET.get('limit', 20))
        offset = int(request.GET.get('offset', 0))
        result = UserService.search_users_by_phone(phone, limit, offset, client_ip=get_client_ip(request))
        return JsonResponse(result)
    except Exception as e:
        logger.error(f"根据手机号搜索用户失败 ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({
            'result': 0,
            'message': '搜索失败'
        }, status=500)
