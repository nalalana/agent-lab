"""
/backend/apps/user/serializers.py
用户序列化器
"""
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from apps.user.models import User
from apps.user.services import SMSService


class UserRegistrationSerializer(serializers.ModelSerializer):
    """用户注册序列化器"""
    password1 = serializers.CharField(write_only=True, min_length=8, max_length=16)
    password2 = serializers.CharField(write_only=True, min_length=8, max_length=16)
    captcha_input = serializers.CharField(write_only=True)
    captcha_key = serializers.CharField(write_only=True)
    
    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2', 'captcha_input', 'captcha_key']
    
    def validate_username(self, value):
        """验证用户名"""
        if not value:
            raise serializers.ValidationError("用户名不允许为空")
        
        # 检查用户名是否已存在且已激活
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("该用户名已存在")
        
        return value
    
    def validate_email(self, value):
        """验证邮箱"""
        if not value:
            raise serializers.ValidationError("邮箱不允许为空")

        # 检查邮箱是否已被注册且已激活
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("该邮箱已经被注册")
        
        return value
    
    def validate(self, data):
        """验证密码一致性"""
        password1 = data.get('password1')
        password2 = data.get('password2')
        
        if password1 != password2:
            raise serializers.ValidationError("两次密码不一致")
        
        # 验证密码强度
        password_validation_result = self._check_password_strength(password1)
        if password_validation_result['result'] != 1:
            raise serializers.ValidationError(password_validation_result['message'])
        
        return data
    
    def _check_password_strength(self, password):
        """检查密码强度"""
        if len(password) < 8 or len(password) > 16:
            return {'result': 0, 'message': '长度需为8-16个字符,请重新输入。'}
        
        # 检查是否包含中文或空格
        for char in password:
            if 0x4e00 <= ord(char) <= 0x9fa5 or ord(char) == 0x20:
                return {'result': 0, 'message': '不能使用空格、中文，请重新输入。'}
        
        # 检查密码复杂度
        has_number = any(c.isnumeric() for c in password)
        has_letter = any('a' <= c <= 'z' or 'A' <= c <= 'Z' for c in password)
        has_special = any(not (c.isnumeric() or 'a' <= c <= 'z' or 'A' <= c <= 'Z') for c in password)
        
        complexity_score = sum([has_number, has_letter, has_special])
        
        if complexity_score >= 2:
            return {'result': 1, 'message': '密码强度合适'}
        else:
            return {'result': 0, 'message': '至少含数字/字母/字符2种组合，请重新输入。'}


class UserLoginSerializer(serializers.Serializer):
    """用户登录序列化器（手机号+密码）"""
    phone = serializers.CharField()
    password = serializers.CharField(error_messages={
        'blank': '密码不能为空',
        'required': '密码不能为空'
    })

    def validate_phone(self, value):
        if not value:
            raise serializers.ValidationError("手机号不能为空")
        if not value.isdigit() or not (6 <= len(value) <= 20):
            raise serializers.ValidationError("手机号格式不正确")
        return value


class PhoneSendCodeSerializer(serializers.Serializer):
    """发送短信验证码序列化器"""
    phone = serializers.CharField(required=True)
    captcha_token = serializers.CharField(required=True)
    scene = serializers.CharField(required=False, allow_blank=True, default="login_sms")

    def validate_phone(self, value):
        if not value:
            raise serializers.ValidationError("手机号不能为空")
        # 简单校验: 仅数字，长度 6-20
        if not value.isdigit() or not (6 <= len(value) <= 20):
            raise serializers.ValidationError("手机号格式不正确")
        return value

    def validate_scene(self, value):
        allowed = {"login_sms", "register_sms", "profile_sms"}
        scene = value or "login_sms"
        if scene not in allowed:
            raise serializers.ValidationError("验证码场景不正确")
        return scene


class TencentCaptchaVerifySerializer(serializers.Serializer):
    ticket = serializers.CharField(required=True)
    randstr = serializers.CharField(required=True)
    scene = serializers.CharField(required=False, allow_blank=True, default="login_sms")

    def validate_scene(self, value):
        allowed = {"login_sms", "register_sms", "profile_sms", "password_login"}
        scene = value or "login_sms"
        if scene not in allowed:
            raise serializers.ValidationError("验证码场景不正确")
        return scene


class PhoneRegisterSerializer(serializers.Serializer):
    """手机号注册序列化器（验证码）"""
    phone = serializers.CharField(required=True)
    code = serializers.CharField(required=True)
    username = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True)
    agreed_to_terms = serializers.BooleanField(required=True)

    def validate_phone(self, value):
        if not value:
            raise serializers.ValidationError("手机号不能为空")
        if not value.isdigit() or not (6 <= len(value) <= 20):
            raise serializers.ValidationError("手机号格式不正确")
        # 已激活用户不允许重复注册
        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("该手机号已注册，请直接登录或使用短信验证码登录")
        return value

    def validate(self, data):
        if not data["agreed_to_terms"]:
            raise serializers.ValidationError("请先阅读并同意用户协议和隐私政策")
        password = data.get("password") or ""
        if password:
            self._validate_password_strength(password)
            data["password"] = password
        return data

    @staticmethod
    def _validate_password_strength(password):
        if len(password) < 8 or len(password) > 16:
            raise serializers.ValidationError("密码长度需为8-16个字符")
        for char in password:
            if 0x4e00 <= ord(char) <= 0x9fa5 or ord(char) == 0x20:
                raise serializers.ValidationError("不能使用空格、中文")
        has_number = any(c.isnumeric() for c in password)
        has_letter = any('a' <= c <= 'z' or 'A' <= c <= 'Z' for c in password)
        has_special = any(not (c.isnumeric() or 'a' <= c <= 'z' or 'A' <= c <= 'Z') for c in password)
        if sum([has_number, has_letter, has_special]) < 2:
            raise serializers.ValidationError("密码强度不合适，至少含数字/字母/字符2种组合")


class PhoneLoginSerializer(serializers.Serializer):
    """手机号登录（验证码）"""
    phone = serializers.CharField(required=True)
    code = serializers.CharField(required=True)
    agreed_to_terms = serializers.BooleanField(required=False, default=False)

    def validate_phone(self, value):
        if not value:
            raise serializers.ValidationError("手机号不能为空")
        if not value.isdigit() or not (6 <= len(value) <= 20):
            raise serializers.ValidationError("手机号格式不正确")
        return value

class UserProfileSerializer(serializers.ModelSerializer):
    """用户资料序列化器"""
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'uid', 'public_id', 'username', 'email', 'phone', 'avatar_url', 'bio', 
            'created_at', 'updated_at', 'role'
        ]
        read_only_fields = ['public_id', 'created_at', 'updated_at']
    
    def get_avatar_url(self, obj):
        """获取头像URL - 使用腾讯云COS临时地址"""
        if obj.avatar:
            try:
                from common.utils.Tengcentcos_utils import get_avatar_presigned_url
                # 使用公共函数生成临时访问URL，有效期1小时
                return get_avatar_presigned_url(obj.avatar, expires_in=3600)
            except Exception as e:
                # 如果生成临时URL失败，返回None或默认头像
                return None
        return None
    
    def validate_avatar(self, value):
        """验证头像URL"""
        if value:
            url_validator = URLValidator()
            try:
                url_validator(value)
            except ValidationError:
                raise serializers.ValidationError("头像URL格式不正确")
        return value


class UserUpdateSerializer(serializers.ModelSerializer):
    """用户信息更新序列化器"""
    username = serializers.CharField(required=False, allow_blank=True)
    email = serializers.CharField(required=False, allow_blank=True)
    avatar = serializers.CharField(required=False, allow_blank=True)
    bio = serializers.CharField(required=False, allow_blank=True)
    
    class Meta:
        model = User
        fields = ['username', 'email', 'avatar', 'bio']
    
    def validate_username(self, value):
        """验证用户名"""
        # 检查用户名是否被其他用户使用
        user_id = self.context.get('user_id')
        if User.objects.filter(username=value).exclude(id=user_id).exists():
            raise serializers.ValidationError("该用户名已被使用")
        
        return value
    
    def validate_email(self, value):
        """验证邮箱"""
        value = value.strip()
        # 空字符串表示用户主动解除邮箱绑定，不参与重复性校验。
        if value == '':
            return value

        # 检查邮箱是否被其他用户使用
        user_id = self.context.get('user_id')
        if User.objects.filter(email=value).exclude(id=user_id).exists():
            raise serializers.ValidationError("该邮箱已被使用")
        
        return value

    
    def _check_password_strength(self, password):
        """检查密码强度"""
        if len(password) < 8 or len(password) > 16:
            return {'result': 0, 'message': '长度需为8-16个字符,请重新输入。'}
        
        # 检查是否包含中文或空格
        for char in password:
            if 0x4e00 <= ord(char) <= 0x9fa5 or ord(char) == 0x20:
                return {'result': 0, 'message': '不能使用空格、中文，请重新输入。'}
        
        # 检查密码复杂度
        has_number = any(c.isnumeric() for c in password)
        has_letter = any('a' <= c <= 'z' or 'A' <= c <= 'Z' for c in password)
        has_special = any(not (c.isnumeric() or 'a' <= c <= 'z' or 'A' <= c <= 'Z') for c in password)
        
        complexity_score = sum([has_number, has_letter, has_special])
        
        if complexity_score >= 2:
            return {'result': 1, 'message': '密码强度合适'}
        else:
            return {'result': 0, 'message': '至少含数字/字母/字符2种组合，请重新输入。'}


class CaptchaSerializer(serializers.Serializer):
    """验证码序列化器"""
    captcha_input = serializers.CharField()
    captcha_key = serializers.CharField()
    
    def validate(self, data):
        """验证验证码"""
        captcha_input = data.get('captcha_input')
        captcha_key = data.get('captcha_key')
        
        if not self._check_captcha(captcha_key, captcha_input):
            raise serializers.ValidationError("验证码错误")
        
        return data
    
    def _check_captcha(self, captcha_key, user_input):
        """检查验证码"""
        from captcha.models import CaptchaStore
        try:
            cs = CaptchaStore.objects.get(hashkey=captcha_key)
            return cs.response == user_input.lower()
        except CaptchaStore.DoesNotExist:
            return False


class TokenRefreshSerializer(serializers.Serializer):
    """Token刷新序列化器"""
    refresh_token = serializers.CharField(required=True)
    
    def validate_refresh_token(self, value):
        """验证token格式"""
        if not value:
            raise serializers.ValidationError("Token不能为空")
        
        # 基本格式验证（JWT格式）
        parts = value.split('.')
        if len(parts) != 3:
            raise serializers.ValidationError("Token格式不正确")
        
        return value


class PhonePasswordChangeSerializer(serializers.Serializer):
    """手机号+验证码修改密码"""
    phone = serializers.CharField(required=True)
    code = serializers.CharField(required=True)
    new_password1 = serializers.CharField(min_length=8, max_length=16)
    new_password2 = serializers.CharField(min_length=8, max_length=16)

    def validate_phone(self, value):
        if not value:
            raise serializers.ValidationError("手机号不能为空")
        if not value.isdigit() or not (6 <= len(value) <= 20):
            raise serializers.ValidationError("手机号格式不正确")
        return value

    def validate_code(self, value):
        if not value:
            raise serializers.ValidationError("验证码不能为空")
        if not value.isdigit() or len(value) != 6:
            raise serializers.ValidationError("验证码格式不正确")
        return value

    def validate_new_password1(self, value):
        """检查密码强度"""
        if len(value) < 8 or len(value) > 16:
            raise serializers.ValidationError("长度需为8-16个字符")
        for char in value:
            if 0x4e00 <= ord(char) <= 0x9fa5 or ord(char) == 0x20:
                raise serializers.ValidationError("不能使用空格、中文")
        # 检查密码复杂度
        has_number = any(c.isnumeric() for c in value)
        has_letter = any('a' <= c <= 'z' or 'A' <= c <= 'Z' for c in value)
        has_special = any(not (c.isnumeric() or 'a' <= c <= 'z' or 'A' <= c <= 'Z') for c in value)
        if has_number + has_letter + has_special < 2:
            raise serializers.ValidationError("密码强度不合适，至少含数字/字母/字符2种组合")
        return value

    def validate_new_password2(self, value):
        """验证新密码是否一致"""
        if value != self.initial_data.get('new_password1'):
            raise serializers.ValidationError("两次输入的密码不一致，请重新输入")
        return value

    def validate(self, data):
        if not SMSService.verify_code(data.get('phone'), data.get('code')):
            raise serializers.ValidationError("验证码错误")
        return data

class TwoStepVerifySerializer(serializers.Serializer):
    """两步验证序列化器"""
    phone = serializers.CharField(required=True)
    code = serializers.CharField(required=True)
    
    def validate_phone(self, value):
        """验证手机号"""
        if not value:
            raise serializers.ValidationError("手机号不能为空")
        if not value.isdigit() or not (6 <= len(value) <= 20):
            raise serializers.ValidationError("手机号格式不正确")
        return value
    
    def validate_code(self, value):
        """验证验证码格式是否正确"""
        if not value:
            raise serializers.ValidationError("验证码不能为空")
        if not value.isdigit() or not (len(value) == 6):
            raise serializers.ValidationError("验证码格式不正确")
        return value
    
    def validate(self, data):
        """验证验证码是否正确"""
        phone = data.get('phone')
        code = data.get('code')
        
        if not SMSService.verify_code(phone, code):
            raise serializers.ValidationError("验证码错误")
        return data
    

class UserSearchSerializer(serializers.Serializer):
    """用户搜索序列化器"""
    user_name = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)
    
    def validate_limit(self, value):
        """验证限制数量"""
        if value < 1 or value > 100:
            raise serializers.ValidationError("限制数量必须在1-100之间")
        return value
    
    def validate_offset(self, value):
        """验证偏移量"""
        if value < 0:
            raise serializers.ValidationError("偏移量不能为负数")
        return value


class UserViewSerializer(serializers.Serializer):
    """查看用户序列化器（按 uid）"""
    uid = serializers.CharField(required=True)
    
    def validate_uid(self, value):
        if not value:
            raise serializers.ValidationError("uid不能为空")
        return value


class AvatarUploadSerializer(serializers.Serializer):
    """头像上传序列化器"""
    avatar = serializers.URLField(required=True)
    
    def validate_avatar(self, value):
        """验证头像URL"""
        if not value:
            raise serializers.ValidationError("头像URL不能为空")
        
        # 验证URL格式
        url_validator = URLValidator()
        try:
            url_validator(value)
        except ValidationError:
            raise serializers.ValidationError("头像URL格式不正确")
        
        return value


class AdminAddSerializer(serializers.Serializer):
    """添加管理员序列化器"""
    username = serializers.CharField(required=True)
    
    def validate_username(self, value):
        """验证用户名"""
        if not value:
            raise serializers.ValidationError("用户名不能为空")
        return value


class AdminDeleteSerializer(serializers.Serializer):
    """删除管理员序列化器"""
    username = serializers.CharField(required=True)
    
    def validate_username(self, value):
        """验证用户名"""
        if not value:
            raise serializers.ValidationError("用户名不能为空")
        return value
