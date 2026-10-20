from rest_framework import serializers


class GateOperationSerializer(serializers.Serializer):
    OPRATION_FOLLOW = 1
    OPRATION_UNFOLLOW = 0
    OPRATION_CHOICES = (
        (OPRATION_FOLLOW, 'follow'),
        (OPRATION_UNFOLLOW, 'unfollow'),
    )
    operation = serializers.ChoiceField(choices=OPRATION_CHOICES)
    gid = serializers.CharField(max_length=255)

class CheckGateFollowingStatusSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=255, required=False, allow_blank=True)
    gid = serializers.CharField(max_length=255)

class GetFollowingGatesSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=255, required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20)
    offset = serializers.IntegerField(default=0)

class CompanyOperationSerializer(serializers.Serializer):
    OPRATION_FOLLOW = 1
    OPRATION_UNFOLLOW = 0
    OPRATION_CHOICES = (
        (OPRATION_FOLLOW, 'follow'),
        (OPRATION_UNFOLLOW, 'unfollow'),
    )
    operation = serializers.ChoiceField(choices=OPRATION_CHOICES)
    cid = serializers.CharField(max_length=255)

class CheckCompanyFollowingStatusSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=255, required=False, allow_blank=True)
    cid = serializers.CharField(max_length=255)

class GetFollowingCompaniesSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=255, required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20)
    offset = serializers.IntegerField(default=0)