"""
/backend/apps/user/models.py
用户模型
"""
from django.db import models
import secrets
from common.utils.Tengcentcos_utils import get_avatar_presigned_url


user_role_choices = (
    ('root', 'root'),
    ('admin', 'admin'),
    ('user', 'user'),
)


PUBLIC_ID_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'


def generate_public_id():
    """Short, non-sequential public identifier; internal UID stays unchanged."""
    return 'U' + ''.join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(12))


class User(models.Model):
    id = models.BigAutoField(primary_key=True)
    uid = models.CharField(max_length=255)
    public_id = models.CharField(max_length=13, unique=True, default=generate_public_id, editable=False)
    username = models.CharField(max_length=255)
    email = models.CharField(max_length=255, null=True)
    phone = models.CharField(max_length=20, null=True)
    user_password = models.CharField('密码', max_length=100, default="", null=True)
    avatar = models.CharField(max_length=512, null=True)
    bio = models.TextField(null=True)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)
    role = models.CharField(default='user', choices=user_role_choices, max_length=10)
    terms_version = models.CharField(max_length=20, null=True)
    privacy_version = models.CharField(max_length=20, null=True)
    consented_at = models.DateTimeField(null=True)
    consent_ip = models.GenericIPAddressField(null=True)

    class Meta:
        db_table = 'user_user'
    
    def get_info(self):
        return {
            'uid': self.uid,
            'public_id': self.public_id,
            'username': self.username,
            'email': self.email,
            'phone': self.phone,
            'avatar': get_avatar_presigned_url(self.avatar) if self.avatar else None,
            'bio': self.bio,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }




class CompanyInvitation(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)
    remaining_uses = models.PositiveIntegerField(default=1)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class CustomerFeedback(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    category = models.CharField(max_length=20)
    rating = models.PositiveSmallIntegerField()
    description = models.TextField()
    contact = models.CharField(max_length=200, blank=True)
    image_keys = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
