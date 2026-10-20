"""
邮件验证相关工具类（从 utils/Sending_utils.py 迁移到 common/utils/Sending_utils.py）
"""
import platform
from random import Random

from django.core.mail import EmailMessage
from django.template import loader
from django.conf import settings

from common.utils.Login_utils import *
from properties import *


def create_code(random_length=6):
    code = ''
    chars = 'AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvWwXxYyZz0123456789'
    length = len(chars) - 1
    random = Random()
    for _ in range(random_length):
        code += chars[random.randint(0, length)]
    return code


def send_email(payload, email, mail_type):
    url = sign_token(payload)
    base_url = getattr(settings, 'BASE_URL', None)
    if not base_url:
        # fallback 到 properties 的环境地址；优先依据 DEBUG 推断
        try:
            base_url = production_base_url if not getattr(settings, 'DEBUG', False) else local_base_url
        except Exception:
            base_url = 'http://127.0.0.1:8000'
    url = f"{base_url}/api/user/email/{url}"

    content = {'url': url}
    email_title = email_body = ''

    if mail_type == 'register':
        email_title = r"欢迎注册透明安全数据空间"
        email_body = loader.render_to_string('EmailContent-register.html', content)
    elif mail_type == 'find':
        email_title = r"透明安全数据空间重设密码"
        email_body = loader.render_to_string('EmailContent-find.html', content)
    try:
        message = EmailMessage(email_title, email_body, EMAIL_HOST_USER, [email])
        message.content_subtype = 'html'
        send_status = message.send()
        return send_status
    except Exception as e:
        print(str(e))
        return 0


