from rest_framework import serializers

from apps.risk_control.models import RiskManualAction, RiskRule


class RiskDecisionLogListSerializer(serializers.Serializer):
    scope = serializers.CharField(required=False, allow_blank=True)
    decision = serializers.ChoiceField(required=False, choices=["allow", "deny"])
    actor_user_id = serializers.CharField(required=False, allow_blank=True)
    company_id = serializers.CharField(required=False, allow_blank=True)
    api_key_id = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(default=0, min_value=0)


class RiskManualActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=[item[0] for item in RiskManualAction.ACTION_CHOICES])
    dimension = serializers.ChoiceField(choices=[item[0] for item in RiskRule.DIMENSION_CHOICES])
    value = serializers.CharField()
    scope = serializers.CharField(required=False, allow_blank=True, default="")
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    expires_in = serializers.IntegerField(required=False, default=0, min_value=0)
    multiplier = serializers.IntegerField(required=False, default=1, min_value=1, max_value=100)
