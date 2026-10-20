from django.contrib import admin
from apps.sso.models import SSOClient, SSOAuthorizationCode


@admin.register(SSOClient)
class SSOClientAdmin(admin.ModelAdmin):
    list_display = ('client_id', 'name', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('client_id', 'name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SSOAuthorizationCode)
class SSOAuthorizationCodeAdmin(admin.ModelAdmin):
    list_display = ('code', 'user', 'company', 'client_id', 'is_used', 'expires_at', 'created_at')
    list_filter = ('is_used', 'created_at', 'expires_at')
    search_fields = ('code', 'user__username', 'company__company_name', 'client_id')
    readonly_fields = ('created_at',)
