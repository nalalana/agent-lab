"""
/backend/common/immudbIO/serializers.py
immudb序列化器
"""
import re
import uuid
from rest_framework import serializers
from common.immudbIO.models import CellCtfaChoices, CellCttChoices


#创建项目序列化器
class CreateProjectSerializer(serializers.Serializer):
    gid = serializers.CharField(required=True)


#创建表格序列化器
class CreateTableSerializer(serializers.Serializer):
    gid = serializers.CharField(required=True)
    tid = serializers.CharField(required=True)
    column = serializers.IntegerField(required=True)

#写入数据序列化器
class WriteDataSerializer(serializers.Serializer):
    cell_id = serializers.CharField(required=False, allow_null=True)
    r = serializers.IntegerField(required=True)
    c = serializers.IntegerField(required=True)
    ctfa = serializers.ChoiceField(choices=CellCtfaChoices.CHOICES, required=False, default=CellCtfaChoices.GENERAL)
    ctt = serializers.ChoiceField(choices=CellCttChoices.CHOICES, required=False, default=CellCttChoices.GENERAL)
    cts = serializers.CharField(required=False, default='')
    bg = serializers.CharField(required=False, default='#FFFFFF')
    ff = serializers.CharField(required=False, default='0')
    fc = serializers.CharField(required=False, default='#000000')
    fs = serializers.IntegerField(required=False, default=10)
    bl = serializers.BooleanField(required=False, default=False)
    it = serializers.BooleanField(required=False, default=False)
    cl = serializers.BooleanField(required=False, default=False)
    un = serializers.BooleanField(required=False, default=False)
    vt = serializers.IntegerField(required=False, default=0, min_value=0, max_value=2)
    ht = serializers.IntegerField(required=False, default=0, min_value=0, max_value=2)
    tb = serializers.IntegerField(required=False, default=0, min_value=0, max_value=2)
    tr = serializers.IntegerField(required=False, default=0, min_value=0, max_value=360)

    def validata_ff(self, value):
        if value not in [str(i) for i in range(13)]:
            raise serializers.ValidationError("字体编号必须在0-12之间")
        return value
    
    def validate_bg(self, value):
        #格式必须是 #RRGGBB
        if not re.match(r'^#([0-9a-fA-F]{6})$', value):
            raise serializers.ValidationError("颜色格式必须是 #RRGGBB")
        return value
    
    def validate_fc(self, value):
        #格式必须是 #RRGGBB
        if not re.match(r'^#([0-9a-fA-F]{6})$', value):
            raise serializers.ValidationError("颜色格式必须是 #RRGGBB")
        return value

    def validate_fs(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("字体大小必须在0-100之间")
        return value
    
    def validate_tr(self, value):
        if value < 0 or value > 360:
            raise serializers.ValidationError("文本旋转角度必须在0-360之间")
        return value
    
    def validate_cell_id(self, value):
        if value is None:
            value = str(uuid.uuid4()).replace('-', '')
        return value
    
#获取数据序列化器
class GetDataSerializer(serializers.Serializer):
    gid = serializers.CharField(required=True)
    tid = serializers.CharField(required=True)
    limit = serializers.IntegerField(required=True)
    offset = serializers.IntegerField(required=True)
