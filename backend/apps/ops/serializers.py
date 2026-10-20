from rest_framework import serializers


class IdListSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False
    )


class PartnerCreateSerializer(serializers.Serializer):
    logo = serializers.CharField(allow_blank=True, required=False)
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    website = serializers.CharField(max_length=255, required=False, allow_blank=True)


