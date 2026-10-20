## 安装

1. 安装 immudb：按照 immudb 仓库文档中提供的安装说明进行安装：[immudb 安装](https://github.com/codenotary/immudb).

2. 使用 pip 在您的虚拟环境中安装：
   ```base
   conda create -n immudb python=3.10
   conda activate immudb
   pip install -r requirements.txt
   ```
___
## 可选配置

#### 在 config/settings 中定义环境配置
当前项目使用分环境配置结构，服务运行至少分为开发测试和线上两套：

- 开发测试环境：`config.settings.dev`，平时开发、联调、内部测试使用
- 线上环境：`config.settings.prod`，正式用户访问使用
- 自动化测试环境：`config.settings.test`，跑 Django 测试时使用

请通过环境变量 `DJANGO_SETTINGS_MODULE` 切换：

```bash
export DJANGO_SETTINGS_MODULE=config.settings.dev   # 开发测试
export DJANGO_SETTINGS_MODULE=config.settings.prod  # 线上
export DJANGO_SETTINGS_MODULE=config.settings.test  # 自动化测试
```

#### 在 settings.py 中定义 Immu 配置
- IMMU_URL = (str) (默认值: 'localhost:3322') *immudb 所在的 URL/IP*。
- IMMU_DEFAULT_EXPIRE_TIME = (dict) (默认值: None) *键/值事务的默认过期时间*。
- IMMU_DEFAULT_DB = (str) (默认值: 'defaultdb') *用于 immu 模型的默认数据库*。
- IMMU_USER = (str) (默认值: 'immudb') *immudb 登录的用户名*。
- IMMU_PASSWORD = (str) (默认值: 'immudb') *immudb 登录的密码*。
- IMMU_PUBLIC_KEY = (str) (默认值: None) *用于 immudb 加密系统的公钥路径*。
___
## 基本用法
#### Immu model key/value
1. 在你的应用models.py文件中导入抽象类和类装饰器：
```base
from immu_django.abc_models import ImmudbKeyField, immu_key_value_class
```

2. 导入Django模型：
```base
from django.db import models
```

3. 创建一个只包含ImmudbKeyField字段的模型类，并使用immu_key_value_class装饰器：
```base
@immu_key_value_class
class ExampleModel(ImmudbKeyField):
    
```

4. 在类中定义模型属性：
```base
@immu_key_value_class
class ExampleModel(ImmudbKeyField):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255, null=True)
    number = models.IntegerField()
```

5. 使用类方法与Immu数据库键/值模型进行交互：
- create = *在Immu数据库中创建一个键值对行*：```ExampleModel.create(key='row_key', name='Jack', number=1)``` ```-> None``` 

- create_mult = *在一个事务中向Immu数据库中创建多个对象*：```ExampleModel.create_mult(obj_list=[{'key': 'row_key', 'values': {'name': 'Jack', 'number': 1}}, ...])``` ```-> None```

- set_ref = *为具有给定键的对象设置参考值*：```ExampleModel.set_ref(key='row_key', ref_key='ref_key')``` ```-> None```

- set_score = *为对象设置集合和分数*：```ExampleModel.set_score(key='row_key', collection='collection_key', score=10.2)``` ```-> None```

- delete = *将具有给定键的对象标记为已删除*：```ExampleModel.delete(key='row_key')``` ```-> None``` 

- after = *获取给定键和事务ID之后的验证行*：```ExampleModel.after(key='row_key', tx_id=1)``` ```-> Dict[key (str), value (dict), tx_id (int), revision (int), verified (bool), timestamp (int), ref_key (str | None)]```

- all = *获取Immu数据库中的所有对象* ```ExampleModel.all()``` ```-> Dict[key (str): value (dict)]```

- get = *获取Immu数据库中的所有对象*：```ExampleModel.get(key_or_ref='row_key_or_row_reference')``` ```-> Dict[key (str), value (dict), tx_id (int), revision (int)]```

- get_score = *基于集合和分数获取行*：```ExampleModel.get_score(colection='collection_key')``` ```-> List[Dict[key (str), value (dict), tx_id (int), revision (int), score(float)], ...]```

- get_tx = *获取具有给定事务ID的所有行键*：```ExampleModel.get_tx(tx_id=1)``` ```-> List['row_key', ...]```

- get_with_tx = *仅使用键和事务ID获取一个已验证的行*：```ExampleModel.get_with_tx(key='row_key', tx_id=1)``` ```-> Dict[key (str), value (dict), tx_id (int), revision (int), verified (bool), timestamp (int), ref_key (str | None)]```

- history = *获取给定键的历史行*：```ExampleModel.history(key='row_key')``` ```-> List[Dict[key (str), value (dict), tx_id (int)]]```

- starts_with = *获取以给定前缀开头的所有对象*：```ExampleModel.starts_with(prefix='row_')``` ```-> Dict[key (str): value (dict)]```

___
#### Immu model sql
1. 在你的应用models.py文件中导入抽象类和类装饰器：
```base
from immu_django.abc_models import ImmudbSQL, immu_sql_class
```

2. 导入Django模型：
```base
from django.db import models
```

3. 创建一个只继承ImmudbSQL的模型类，并使用immu_sql_class装饰器：
```base
@immu_sql_class
class ExampleModel(ImmudbSQL):
    
```

4. 在类中定义模型属性：
```base
@immu_sql_class
class ExampleModel(ImmudbSQL):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255, null=True)
    number = models.IntegerField()
```

5. 使用类方法与Immu模型SQL进行交互：
- create = *在此类SQL表中插入一个对象的事务*：```ExampleModel.create(name='Jack', number=1)``` ```-> SQLModel```
- create_mult = *在此类SQL表中插入多个对象的事务*：```ExampleModel.create_mult([{'name':'Jack', 'number':1}, ...])``` ```-> list[SQLModel, ...]```
- all = *在此类SQL表中搜索一个对象*：```ExampleModel.all()``` ```-> list[SQLModel, ...]```
- get = *获取表中的所有对象*：```ExampleModel.get(**kwargs)``` ```-> SQLModel```
- filter = *获取表中与给定参数匹配的所有对象*：```ExampleModel.filter(**kwargs)``` ```-> list[SQLModel, ...]```
