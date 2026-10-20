from rest_framework import serializers
from .models import Principal, Resource, Grant


class PrincipalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Principal
        fields = ['id', 'type', 'external_id']


class ResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Resource
        fields = ['id', 'type', 'external_id', 'parent']


class GrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Grant
        fields = ['id', 'principal', 'resource', 'permissions', 'granter', 'created_at', 'expires_at', 'inherit']


class EffectivePermissionRequestSerializer(serializers.Serializer):
    principal_type = serializers.ChoiceField(choices=[c[0] for c in Principal.TYPE_CHOICES])
    principal_external_id = serializers.CharField()
    resource_external_id = serializers.CharField(required=False)
    resource_uid = serializers.CharField(required=False)

    def validate(self, attrs):
        if not attrs.get('resource_external_id') and not attrs.get('resource_uid'):
            raise serializers.ValidationError('resource_external_id required')
        return attrs


class GrantRequestSerializer(serializers.Serializer):
    principal_type = serializers.ChoiceField(choices=[c[0] for c in Principal.TYPE_CHOICES])
    principal_external_id = serializers.CharField()
    resource_external_id = serializers.CharField(required=False)
    resource_uid = serializers.CharField(required=False)
    permissions = serializers.ChoiceField(choices=[c[0] for c in Grant.PERM_CHOICES])
    inherit = serializers.BooleanField(default=True)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get('resource_external_id') and not attrs.get('resource_uid'):
            raise serializers.ValidationError('resource_external_id required')
        return attrs


class CheckAccessRequestSerializer(serializers.Serializer):
    resource_type = serializers.ChoiceField(choices=[c[0] for c in Resource.TYPE_CHOICES])
    resource_external_id = serializers.CharField()
    permission = serializers.ChoiceField(
        choices=[
            ('read', Grant.PERM_READ),
            ('write', Grant.PERM_WRITE),
            ('admin', Grant.PERM_ADMIN),
            ('super_admin', Grant.PERM_SUPER_ADMIN),
            (Grant.PERM_READ, Grant.PERM_READ),
            (Grant.PERM_WRITE, Grant.PERM_WRITE),
            (Grant.PERM_ADMIN, Grant.PERM_ADMIN),
            (Grant.PERM_SUPER_ADMIN, Grant.PERM_SUPER_ADMIN),
        ],
        default='read',
    )
