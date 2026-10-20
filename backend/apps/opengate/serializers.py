"""
Serializer layer for the opengate app.

Each serializer validates incoming request payloads for views so that the
service layer can assume well-formed, typed inputs. Keep validation error
messages concise for client display while being strict about types and
required fields.
"""

from rest_framework import serializers


class CreateGateSerializer(serializers.Serializer):
    """Payload to create a Gate."""
    gatename = serializers.CharField(max_length=255)
    cid = serializers.CharField(max_length=40, required=False, allow_blank=False)
    description = serializers.CharField(required=False, allow_blank=True)
    logo = serializers.CharField(required=False, allow_blank=True)
    background_image = serializers.CharField(required=False, allow_blank=True)


class APIKeyCreateSerializer(serializers.Serializer):
    """(Reserved) Payload to create an API key with a friendly name."""
    name = serializers.CharField(max_length=255)


class GateQuerySerializer(serializers.Serializer):
    """Query gates with optional keyword and pagination."""
    name = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class ListTableSerializer(serializers.Serializer):
    """List all tables under a gate."""
    gid = serializers.CharField()
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)

class ListTableByUserSerializer(serializers.Serializer):
    """List all tables under a user."""
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class TableDetailsSerializer(serializers.Serializer):
    """Fetch table metadata and structure for a given gate/table."""
    tid = serializers.CharField()


class TableDataSerializer(serializers.Serializer):
    """Query table data with optional projection and simple pagination."""
    tid = serializers.CharField()
    what_to_select = serializers.ListField(child=serializers.CharField(), required=False)
    params = serializers.DictField(required=False)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=1000)
    offset = serializers.IntegerField(default=0, min_value=0)


class TableAuditSerializer(serializers.Serializer):
    tid = serializers.CharField()
    source = serializers.ChoiceField(choices=['web', 'api_key'], required=False)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class CellEditSerializer(serializers.Serializer):
    """Single-cell edit payload for simplified table editing."""
    r = serializers.IntegerField(min_value=0)
    c = serializers.IntegerField(min_value=0)
    value = serializers.JSONField(required=False)
    expected_version = serializers.IntegerField(required=False, min_value=0)


class UpsertTableDataSerializer(serializers.Serializer):
    """Upsert table data payload."""
    tid = serializers.CharField()
    values = serializers.DictField(allow_empty=False)


class SimpleUpsertTableDataSerializer(serializers.Serializer):
    """Simplified upsert payload accepted by /opengate/tables/data/upsert."""
    tid = serializers.CharField()
    values = serializers.ListField(child=CellEditSerializer(), allow_empty=False)
    append = serializers.BooleanField(default=False)
    expected_max_row_index = serializers.IntegerField(required=False, min_value=-1)
    expected_table_version = serializers.IntegerField(required=False, min_value=0)

    def validate(self, attrs):
        if attrs['append'] and 'expected_max_row_index' not in attrs:
            raise serializers.ValidationError('新增行需要 expected_max_row_index')
        return attrs


class DeleteTableRowSerializer(serializers.Serializer):
    tid = serializers.CharField()
    r = serializers.IntegerField(min_value=0)


class ListTableMemberSerializer(serializers.Serializer):
    """Paginated listing of table members."""
    tid = serializers.CharField()
    limit = serializers.IntegerField(default=20, min_value=1, max_value=200)
    offset = serializers.IntegerField(default=0, min_value=0)


class OperateInvitationSerializer(serializers.Serializer):
    """Accept or reject an invitation."""
    invitation_id = serializers.IntegerField(min_value=1)
    operation = serializers.ChoiceField(choices=['agree', 'reject'])


class TransferOwnerSerializer(serializers.Serializer):
    """Transfer gate ownership to another activated user."""
    gatename = serializers.CharField()
    new_owner = serializers.CharField()


class AppTableDetailsSerializer(serializers.Serializer):
    """App API-key: table info request."""
    tid = serializers.CharField()


class AppTableDataSerializer(serializers.Serializer):
    """App API-key: table data query with pagination."""
    tid = serializers.CharField()
    what_to_select = serializers.ListField(child=serializers.CharField(), required=False)
    params = serializers.DictField(required=False)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=1000)
    offset = serializers.IntegerField(default=0, min_value=0)


class AppCreateTableSerializer(serializers.Serializer):
    """App API-key: create a table inside the key's bound project."""
    tableName = serializers.CharField(max_length=128)
    columnTitles = serializers.ListField(
        child=serializers.CharField(max_length=128, allow_blank=False),
        allow_empty=False,
        max_length=100,
    )
    numColumns = serializers.IntegerField(required=False, min_value=1, max_value=100)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    permission = serializers.IntegerField(default=0, min_value=0, max_value=2)

    def validate(self, attrs):
        column_count = len(attrs["columnTitles"])
        attrs["numColumns"] = attrs.get("numColumns") or column_count
        if attrs["numColumns"] != column_count:
            raise serializers.ValidationError("numColumns must match columnTitles length")
        return attrs


class AppUpsertTableDataSerializer(serializers.Serializer):
    """App API-key: upsert payload for insertToTable."""
    tid = serializers.CharField()
    values = serializers.ListField(child=CellEditSerializer(), allow_empty=False, max_length=1000)
    append = serializers.BooleanField(default=False)
    expected_max_row_index = serializers.IntegerField(required=False, min_value=-1)
    expected_table_version = serializers.IntegerField(required=False, min_value=0)

    def validate(self, attrs):
        if attrs['append'] and 'expected_max_row_index' not in attrs:
            raise serializers.ValidationError('新增行需要 expected_max_row_index')
        return attrs


class CheckInvitationSerializer(serializers.Serializer):
    """Paginated request for pending invitations."""
    limit = serializers.IntegerField(default=20, min_value=1, max_value=200)
    offset = serializers.IntegerField(default=0, min_value=0)


class UpdateMemberChangeSerializer(serializers.Serializer):
    """Single membership change descriptor."""
    email = serializers.EmailField()
    permission = serializers.ChoiceField(choices=['admin','read','write','invalid'])


class UpdateMemberSerializer(serializers.Serializer):
    """Batch membership changes payload for a table."""
    from apps.iam.models import Grant
    PERM_CHOICES = (
        (Grant.PERM_ADMIN, 'admin'),
        (Grant.PERM_READ, 'read'),
        (Grant.PERM_WRITE, 'write'),
    )
    tid = serializers.CharField()
    uid = serializers.CharField()
    permission = serializers.ChoiceField(choices=PERM_CHOICES)


class RemoveMemberSerializer(serializers.Serializer):
    """Remove one member from a table."""
    tid = serializers.CharField()
    uid = serializers.CharField()



class CreateTableFieldSerializer(serializers.Serializer):
    """Single table field definition for DDL generation."""
    name = serializers.CharField()
    type = serializers.ChoiceField(choices=['INTEGER','BOOLEAN','VARCHAR','FILE','TIMESTAMP','FLOAT'])


class CreateTableSerializer(serializers.Serializer):
    """Create table payload with column titles and metadata."""
    gid = serializers.CharField()
    tableName = serializers.CharField()
    columnTitles = serializers.ListField(child=serializers.CharField(allow_blank=False), allow_empty=False)
    numColumns = serializers.IntegerField(min_value=1)
    description = serializers.CharField(required=False, allow_blank=True)
    permission = serializers.IntegerField(min_value=0, max_value=2)  # 0=private, 1=read, 2=write


class UpdateGateSerializer(serializers.Serializer):
    gid = serializers.CharField()
    gatename = serializers.CharField(required=False, allow_blank=True)
    logo = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    background_image = serializers.CharField(required=False, allow_blank=True)


class ProjectPublicAccessSerializer(serializers.Serializer):
    gid = serializers.CharField()
    public_access = serializers.ChoiceField(
        choices=['internal', 'public_read', 'public_write'],
    )


class DeleteGateSerializer(serializers.Serializer):
    gid = serializers.CharField()

class ListGateSerializer(serializers.Serializer):
    uid = serializers.CharField()
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)

class SearchGateSerializer(serializers.Serializer):
    keyword = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)

class UpdateTableSerializer(serializers.Serializer):
    """Update table payload with column titles and metadata."""
    tid = serializers.CharField(required=True)
    tableName = serializers.CharField(required=False, allow_blank=False)
    columnTitles = serializers.ListField(child=serializers.CharField())
    numColumns = serializers.IntegerField(min_value=1, max_value=26, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    permission = serializers.IntegerField(min_value=0, max_value=2)  # 0=private, 1=read, 2=write


class SearchGateSerializer(serializers.Serializer):
    keyword = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class CreateGateKeySerializer(serializers.Serializer):
    gid = serializers.CharField()
    key_name = serializers.CharField()


class DeleteGateKeySerializer(serializers.Serializer):
    gid = serializers.CharField()
    secret_id = serializers.CharField()


class ListGateKeySerializer(serializers.Serializer):
    gid = serializers.CharField()

class ToggleGateKeySerializer(serializers.Serializer):
    gid = serializers.CharField()
    secret_id = serializers.CharField()
