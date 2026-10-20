from django.db import models
from apps.user.models import User
from apps.opengate.models import Gates
from apps.company.models import Company

class FollowingGate(models.Model):
    """用户关注Gate关系模型"""
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='following_gates')
    following_opengate = models.ForeignKey(
        to=Gates,
        on_delete=models.CASCADE,
        related_name='following_gate_relations'  # avoid clash with Gates.followers field
    )
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'opengate_followinggate'
        unique_together = ('user', 'following_opengate')


class FollowingCompany(models.Model):
    """用户关注Company关系模型"""
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='following_companies')
    following_company = models.ForeignKey(
        to=Company,
        on_delete=models.CASCADE,
        related_name='following_company_relations'  # avoid clash with Company.followers field
    )
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'company_followingcompany'
        unique_together = ('user', 'following_company')
