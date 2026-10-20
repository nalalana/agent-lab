"""
/backend/apps/opengate/models.py
OpenGate模型
"""
from django.db import models
from django.forms import NullBooleanField
from apps.user.models import User
from common.utils.Tengcentcos_utils import get_moderated_image_presigned_url, normalize_cos_key

gates_member_permission_choices = (
    ('admin', 'admin'),
    ('read', 'read'),
    ('write', 'write'),
)

gates_table_permission_choices = (
    ('private', 'private'),
    ('read', 'read'),
    ('write', 'write')
)


class Gates(models.Model):
    LABEL_PERSONAL = 0
    LABEL_COMPANY = 1
    LABEL_ORGANIZATION = 2
    LABEL_CHOICES = (
        (LABEL_PERSONAL, '个人项目'),
        (LABEL_COMPANY, '公司项目'),
        (LABEL_ORGANIZATION, '组织项目'),
    )
    PUBLIC_ACCESS_INTERNAL = 'internal'
    PUBLIC_ACCESS_READ = 'public_read'
    PUBLIC_ACCESS_WRITE = 'public_write'
    PUBLIC_ACCESS_CHOICES = (
        (PUBLIC_ACCESS_INTERNAL, '仅内部可见'),
        (PUBLIC_ACCESS_READ, '外部可读不可写'),
        (PUBLIC_ACCESS_WRITE, '外部可读可写'),
    )
    id = models.BigAutoField(primary_key=True)
    gid = models.CharField(max_length=128, unique=True)
    label = models.IntegerField(choices=LABEL_CHOICES, default=LABEL_PERSONAL, null=True)
    public_access = models.CharField(
        max_length=16,
        choices=PUBLIC_ACCESS_CHOICES,
        default=PUBLIC_ACCESS_INTERNAL,
    )
    description = models.TextField(null=True)
    owner = models.ForeignKey(to=User, on_delete=models.CASCADE)
    gatename = models.CharField(max_length=128)
    logo = models.TextField(null=True)
    background_image = models.TextField(null=True)
    followers = models.IntegerField(default=0)
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    class Meta:
        db_table = 'opengate_gates'

    def __str__(self):
        return self.gatename

    def allows_public_read(self):
        return self.public_access in {
            self.PUBLIC_ACCESS_READ,
            self.PUBLIC_ACCESS_WRITE,
        }

    def allows_public_write(self):
        return self.public_access == self.PUBLIC_ACCESS_WRITE
    
    def get_info(self):
        # 库中可能是历史完整 URL，先规范为 COS key 再生成 presigned URL，避免出现"域名+编码URL"的错误链接
        logo_key = normalize_cos_key(self.logo) if self.logo else None
        bg_key = normalize_cos_key(self.background_image) if self.background_image else None
        return {
            'gid': self.gid,
            'gatename': self.gatename,
            'description': self.description,
            'logo': get_moderated_image_presigned_url(logo_key) if logo_key else None,
            'label': self.label,
            'public_access': self.public_access,
            'background_image': get_moderated_image_presigned_url(bg_key) if bg_key else None,
            'followers': self.followers,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'owner': self.owner.get_info(),
        }


class GatesTable(models.Model):
    """
    用户创建的自定义表格模型
    """
    PERM_PRIVATE = 0
    PERM_READ = 1
    PERM_WRITE = 2
    PERM_CHOICES = (
        (PERM_PRIVATE, 'private'),
        (PERM_READ, 'read'),  
        (PERM_WRITE, 'write'),
    )
    #基本信息
    id = models.BigAutoField(primary_key=True)
    tid = models.CharField(max_length=128, unique=True)
    description = models.TextField(null=True)
    gate = models.ForeignKey(to=Gates, on_delete=models.CASCADE)
    name = models.CharField(max_length=128)

    #权限
    permission = models.IntegerField(default=0, choices=PERM_CHOICES)
    #创建时间
    created_at = models.CharField(max_length=20)
    updated_at = models.CharField(max_length=20)

    #表格配置
    num_columns = models.PositiveIntegerField(default=0)

    #列配置(json格式存储所有列信息)
    columns_config = models.JSONField(verbose_name='列配置', default=dict)

    #自定义管理器
    objects = models.Manager()

    class Meta:
        db_table = 'opengate_gatestable'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['gate', 'created_at'], name='gate_created_idx'),
            models.Index(fields=['name'], name='name_idx'),
        ]

    def __str__(self):
        return self.name

    def get_columns_count(self):
        """获取列数"""
        return self.num_columns
    
    def get_columns(self):
        """获取列配置列表"""
        return self.columns_config if self.columns_config else []
    
    def get_field_names(self):
        """获取字段名列表"""
        return [col.get('field_name', '') for col in self.get_columns()]
    
    def get_column_titles(self):
        """获取列标题列表"""
        return [col.get('title', '') for col in self.get_columns()]
    
    def get_field_mapping(self):
        """获取字段映射关系"""
        mapping = []
        for i, col in enumerate(self.get_columns()):
            mapping.append({
                'field': col.get('field_name', ''),
                'title': col.get('title', ''),
                'index': i
            })
        return mapping
    
    def add_column(self, field_name, title, column_index=None, **kwargs):
        """添加列"""
        if not self.columns_config:
            self.columns_config = []
        
        if column_index is None:
            column_index = len(self.columns_config)
        
        column_config = {
            'field_name': field_name,
            'title': title,
            'column_index': column_index,
            'width': kwargs.get('width', 140),
            'data_type': kwargs.get('data_type', 'text'),
            'validation_rules': kwargs.get('validation_rules', {})
        }
        
        self.columns_config.append(column_config)
        self.num_columns = len(self.columns_config)
        self.save()
    
    def update_column(self, field_name, **kwargs):
        """更新列配置"""
        if not self.columns_config:
            return False
        
        for col in self.columns_config:
            if col.get('field_name') == field_name:
                col.update(kwargs)
                self.save()
                return True
        return False
    
    def remove_column(self, field_name):
        """删除列"""
        if not self.columns_config:
            return False
        
        original_length = len(self.columns_config)
        self.columns_config = [col for col in self.columns_config if col.get('field_name') != field_name]
        
        if len(self.columns_config) < original_length:
            self.num_columns = len(self.columns_config)
            self.save()
            return True
        return False
    
    def get_column_by_field(self, field_name):
        """根据字段名获取列配置"""
        for col in self.get_columns():
            if col.get('field_name') == field_name:
                return col
        return None
    
    def get_column_by_index(self, index):
        """根据索引获取列配置"""
        columns = self.get_columns()
        if 0 <= index < len(columns):
            return columns[index]
        return None
    
    def get_info(self):
        """获取表格信息字典，用于JSON序列化"""
        return {
            'tid': self.tid,
            'name': self.name,
            'description': self.description or '',
            'permission': self.permission,
            'numColumns': self.num_columns,
            'columnTitles': self.get_column_titles(),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class APIKey(models.Model):
    """API Key模型"""
    IS_ACTIVE_ACTIVE = 1
    IS_ACTIVE_INACTIVE = 0
    IS_ACTIVE_CHOICES = (
        (IS_ACTIVE_ACTIVE, 'active'),
        (IS_ACTIVE_INACTIVE, 'inactive'),
    )
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(to=User, on_delete=models.CASCADE, null=True)
    gate = models.ForeignKey(to=Gates, on_delete=models.CASCADE)
    secret_id = models.CharField(max_length=64, unique=True)
    key_name = models.CharField(max_length=128)
    secret_verification = models.CharField(max_length=128)
    created_at = models.CharField(max_length=20)
    is_active = models.IntegerField(default=IS_ACTIVE_ACTIVE, choices=IS_ACTIVE_CHOICES)
    last_used_at = models.CharField(max_length=20, null=True, blank=True)
    last_used_ip = models.CharField(max_length=64, null=True, blank=True)
    last_used_path = models.CharField(max_length=255, null=True, blank=True)
    usage_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'opengate_apikey'

    def toggle_status(self):
        if self.is_active == self.IS_ACTIVE_ACTIVE:
            self.is_active = self.IS_ACTIVE_INACTIVE
        else:
            self.is_active = self.IS_ACTIVE_ACTIVE
        self.save()

    def get_info(self):
        return {
            'secret_id': self.secret_id,
            'key_name': self.key_name,
            'created_at': self.created_at,
            'is_active': self.is_active,
            'last_used_at': self.last_used_at,
            'last_used_ip': self.last_used_ip,
            'last_used_path': self.last_used_path,
            'usage_count': self.usage_count,
        }


class TableAuditEvent(models.Model):
    SOURCE_WEB = 'web'
    SOURCE_API_KEY = 'api_key'
    RESULT_PENDING = 'pending'
    RESULT_SUCCESS = 'success'
    RESULT_FAILED = 'failed'

    table = models.ForeignKey(GatesTable, on_delete=models.CASCADE, related_name='audit_events')
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    api_key = models.ForeignKey(APIKey, null=True, on_delete=models.SET_NULL)
    api_key_name = models.CharField(max_length=128, blank=True, default='')
    source = models.CharField(max_length=16)
    action = models.CharField(max_length=32)
    result = models.CharField(max_length=16, default=RESULT_PENDING)
    row_index = models.PositiveIntegerField(null=True, blank=True)
    column_index = models.PositiveIntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'opengate_table_audit_event'
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['table', '-created_at'], name='table_audit_table_time_idx')]
