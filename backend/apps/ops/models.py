from django.db import models

from apps.company.models import Company
from apps.opengate.models import Gates
from apps.user.models import User



class PopularGate(models.Model):
    id = models.BigAutoField(primary_key=True)
    gate = models.ForeignKey(to=Gates, on_delete=models.CASCADE)

    class Meta:
        db_table = 'ops_populargate'


class PopularCompany(models.Model):
    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(to=Company, on_delete=models.CASCADE)

    class Meta:
        db_table = 'ops_popularcompany'


class Partner(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255)
    logo = models.CharField(max_length=255)
    description = models.CharField(max_length=2000)
    website = models.CharField(max_length=255)
    created_at = models.FloatField()
    updated_at = models.FloatField()

    class Meta:
        db_table = 'ops_partner'


class UserFollow(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='user_follows')
    target = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='user_targets')
    created_at = models.FloatField()

    class Meta:
        db_table = 'ops_userfollow'



class GateFollow(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='gate_follows')
    target = models.ForeignKey(to=Gates, on_delete=models.CASCADE, related_name='gate_followers')
    created_at = models.FloatField()

    class Meta:
        db_table = 'ops_gatefollow'


class CompanyFollow(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, related_name='company_follows')
    target = models.ForeignKey(to=Company, on_delete=models.CASCADE, related_name='company_followers')
    created_at = models.FloatField()

    class Meta:
        db_table = 'ops_companyfollow'


