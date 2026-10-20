"""Company domain validators shared by API and service entry points."""

import re

from django.core.exceptions import ValidationError


SOCIAL_CREDIT_CODE_CHARS = '0123456789ABCDEFGHJKLMNPQRTUWXY'
SOCIAL_CREDIT_CODE_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
SOCIAL_CREDIT_CODE_PATTERN = re.compile(r'^[0-9ABCDEFGHJKLMNPQRTUWXY]{18}$')


def normalize_social_credit_code(value):
    return re.sub(r'[\s-]+', '', str(value or '')).upper()


def is_valid_social_credit_code(value):
    code = normalize_social_credit_code(value)
    if not SOCIAL_CREDIT_CODE_PATTERN.fullmatch(code):
        return False
    total = sum(
        SOCIAL_CREDIT_CODE_CHARS.index(code[index]) * weight
        for index, weight in enumerate(SOCIAL_CREDIT_CODE_WEIGHTS)
    )
    check_index = (31 - total % 31) % 31
    return SOCIAL_CREDIT_CODE_CHARS[check_index] == code[-1]


def validate_social_credit_code(value, *, allow_blank=False):
    code = normalize_social_credit_code(value)
    if allow_blank and not code:
        return ''
    if not is_valid_social_credit_code(code):
        raise ValidationError('请输入有效的18位统一社会信用代码')
    return code
