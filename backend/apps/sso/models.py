from django.db import models
from apps.user.models import User
from apps.company.models import Company


class SSOAuthorizationCode(models.Model):
    """SSO authorization code model for external applications"""

    code = models.CharField(max_length=255, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    client_id = models.CharField(max_length=255)
    redirect_uri = models.CharField(max_length=500)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sso_authorization_codes'
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"Code for {self.user.username} ({self.client_id})"


class SSOClient(models.Model):
    """Registered SSO clients (external applications)"""

    client_id = models.CharField(max_length=255, unique=True)
    client_secret = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    redirect_uris = models.TextField(help_text="Comma-separated allowed redirect URIs")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sso_clients'

    def __str__(self):
        return self.name

    def get_redirect_uris(self):
        """Get list of allowed redirect URIs"""
        return [uri.strip() for uri in self.redirect_uris.split(',') if uri.strip()]
