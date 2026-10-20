from rest_framework import serializers


class SSOExchangeCodeSerializer(serializers.Serializer):
    """Serializer for code exchange request"""
    code = serializers.CharField(required=True)
    client_id = serializers.CharField(required=False, allow_blank=True)
    client_secret = serializers.CharField(required=False, allow_blank=True)


class SSOAuthorizeSerializer(serializers.Serializer):
    """Serializer for authorization request"""
    client_id = serializers.CharField(required=True)
    redirect_uri = serializers.URLField(required=True)
    state = serializers.CharField(required=False, allow_blank=True)
    response_type = serializers.CharField(required=False, default='code')
    company_id = serializers.CharField()
