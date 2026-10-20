from rest_framework import serializers


class ImageModerationStatusSerializer(serializers.Serializer):
    storage_key = serializers.CharField(required=True)


class ImageModerationManualReviewSerializer(serializers.Serializer):
    storage_key = serializers.CharField(required=True)
    action = serializers.ChoiceField(choices=["approve", "reject"])
    reason = serializers.CharField(required=False, allow_blank=True)


class TextModerationCheckSerializer(serializers.Serializer):
    text = serializers.CharField(required=True, allow_blank=True)
    source = serializers.CharField(required=False, allow_blank=True, default="manual.check")
    source_field = serializers.CharField(required=False, allow_blank=True, default="text")
    owner_type = serializers.CharField(required=False, allow_blank=True, default="")
    owner_id = serializers.CharField(required=False, allow_blank=True, default="")


class TextModerationStatusSerializer(serializers.Serializer):
    asset_id = serializers.IntegerField(required=False)
    content_hash = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("asset_id") and not attrs.get("content_hash"):
            raise serializers.ValidationError("asset_id or content_hash is required")
        return attrs


class TextModerationManualReviewSerializer(serializers.Serializer):
    asset_id = serializers.IntegerField(required=True)
    action = serializers.ChoiceField(choices=["approve", "reject"])
    reason = serializers.CharField(required=False, allow_blank=True)
