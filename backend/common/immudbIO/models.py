"""
/backend/common/immudbIO/models.py
immudb模型
"""

# 工作表模型，每次新建项目后在项目内部新建工作表模型
Worksheet = [
    "worksheet_id VARCHAR(64) NOT NULL",  
    "hide BOOLEAN", #  DEFAULT FALSE
    "default_row INTEGER", #  DEFAULT 30
    "default_column INTEGER", #  DEFAULT 24
    "color VARCHAR(64)", #  DEFAULT ''
    "defaultRowHeight INTEGER", #  DEFAULT 20
    "defaultColWidth INTEGER", #  DEFAULT 80
    "deleteFlag BOOLEAN", #  DEFAULT FALSE
    "PRIMARY KEY (worksheet_id)"
]

# 工作表单元格模型，每次新建工作表后在工作表内部新建单元格模型
# 注意：immudb不支持显式外键约束，worksheet_id 存储 Worksheet 表的 worksheet_id 值
# 通过 JOIN 语句关联：SELECT * FROM Cell INNER JOIN Worksheet ON Cell.worksheet_id = Worksheet.worksheet_id

class CellCtfaChoices():
    GENERAL = 'General'
    AT = '@'
    ZERO = '0'
    ZERO_POINT_ZERO = '0.0'
    ZERO_POINT_ZERO_ZERO = '0.00'
    ZERO_POINT_ZERO_ZERO_ZERO = '0.000'
    ZERO_POINT_ZERO_ZERO_ZERO_ZERO = '0.0000'
    CHOICES = [
        (GENERAL, 'General'),
        (AT, '@'),
        (ZERO, '0'),
        (ZERO_POINT_ZERO, '0.0'),
        (ZERO_POINT_ZERO_ZERO, '0.00'),
        (ZERO_POINT_ZERO_ZERO_ZERO, '0.000'),
        (ZERO_POINT_ZERO_ZERO_ZERO_ZERO, '0.0000'),
    ]

class CellCttChoices():
    GENERAL = 'g'
    STRING = 's'
    NUMBER = 'n'
    DATE = 'd'
    TIME = 't'
    DATE_TIME = 'dt'
    BOOLEAN = 'b'
    ERROR = 'e'
    FORMULA = 'f'
    HYPERLINK = 'h'
    COMMENT = 'c'
    DATA_VALIDATION = 'v'
    CHOICES = [
        (GENERAL, 'g'),
        (STRING, 's'),
        (NUMBER, 'n'),
        (DATE, 'd'),
        (TIME, 't'),
        (DATE_TIME, 'dt'),
        (BOOLEAN, 'b'),
        (ERROR, 'e'),
        (FORMULA, 'f'),
        (HYPERLINK, 'h'),
        (COMMENT, 'c'),
        (DATA_VALIDATION, 'v'),
    ]

Cell = [
    "cell_id VARCHAR(64) NOT NULL",
    "worksheet_id VARCHAR(64) NOT NULL",  # 外键：引用 Worksheet.worksheet_id，用于 JOIN 关联
    "r INTEGER NOT NULL",             # 行
    "c INTEGER NOT NULL",             # 列
    # ctfa + ctt ==>  ct:{fa,t} 这两个字段共同构成 celldata.ct 字段的值，决定了如何显示单元格内容
    "ctfa VARCHAR(64)", # DEFAULT '' 内容格式，ct:{fa} Format格式的定义串，'General'|'@'|'0'|'0.0'.... 
    "ctt VARCHAR(32)", # DEFAULT '' ct:{t} Type类型,'g'|'s'|'n'....
    "cts VARCHAR(15360)",# DEFAULT '' ct:{s} inlineStr v 内容整体存储
    "bg VARCHAR(16)", # DEFAULT '#FFFFFF' 背景颜色 #FFFFFF 白色
    "ff VARCHAR(64)", # DEFAULT '0' 字体, 0 Times New Roman、 1 Arial、2 Tahoma 、3 Verdana、4 微软雅黑、5 宋体（Song）、6 黑体（ST Heiti）、7 楷体（ST Kaiti）、 8 仿宋（ST FangSong）、9 新宋体（ST Song）、10 华文新魏、11 华文行楷、12 华文隶书
    "fc VARCHAR(16)", # DEFAULT '#000000' 颜色 #000000 黑色
    "fs INTEGER", # DEFAULT 10 字体大小
    "bl BOOLEAN", # DEFAULT FALSE 是否加粗
    "it BOOLEAN", # DEFAULT FALSE 是否斜体
    "cl BOOLEAN", # DEFAULT FALSE 是否删除线
    "un BOOLEAN", # DEFAULT FALSE 是否下划线
    "vt INTEGER", # DEFAULT 0 垂直对齐方式 0 中间对齐、1 顶部对齐、2 底部对齐
    "ht INTEGER", # DEFAULT 0 水平对齐方式 0 中间对齐、1 左对齐、2 右对齐
    "tb INTEGER", # DEFAULT 0 文本换行方式，0 自动换行、1 单行、2 多行
    "tr INTEGER", # DEFAULT 0 文本旋转角度，0 不旋转、90 顺时针旋转90度、180 顺时针旋转180度、270 顺时针旋转270度
    "PRIMARY KEY (cell_id)"
]