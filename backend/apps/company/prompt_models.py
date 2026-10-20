"""Versioned company-level prompts for the unified Agent runtime."""

from django.db import models

from apps.user.models import User

from .models import TransparentCompany


class TransparentCompanyPromptVersion(models.Model):
    """Immutable, auditable versions of the company-level prompt."""

    STATUS_DRAFT = 'draft'
    STATUS_ACTIVE = 'active'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'draft'),
        (STATUS_ACTIVE, 'active'),
        (STATUS_ARCHIVED, 'archived'),
    ]

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(
        to=TransparentCompany,
        on_delete=models.CASCADE,
        related_name='prompt_versions',
    )
    version = models.PositiveIntegerField()
    content = models.TextField(blank=True, default='')
    checksum = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    created_by = models.ForeignKey(
        to=User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transparent_company_prompt_versions',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'company_transparentcompanypromptversion'
        ordering = ['-version']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'version'],
                name='cmp_company_prompt_version_uniq',
            ),
            models.UniqueConstraint(
                fields=['company'],
                condition=models.Q(status='active'),
                name='cmp_company_prompt_active_uniq',
            ),
        ]
