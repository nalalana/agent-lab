# PostgreSQL并发测试入口与环境预检

日期：2026-10-21。结论：入口已准备，真实PostgreSQL锁场景未运行，不能对锁保护效果下集成结论。

## 环境与实际运行

当前PATH未发现docker、podman、psql；未发现PostgreSQL或Docker Windows服务，也没有AGENT_LAB专用数据库环境变量。后端虚拟环境已有psycopg2，未安装新依赖或服务，未读取生产数据库连接或模型密钥。

运行run_postgres_locks.py的预检保存在忽略目录lab/results/postgres-lock-preflight-1/summary.json：status=unavailable、checks_passed=0、postgresql_lock_behavior_tested=false。缺显式启用配置，未发起数据库连接，也未运行两个锁案例。

本轮90项本地检查通过：原图基线82、版本合同5、入口失败处理3。3项新增检查仅验证未启用、缺密码及连接失败时拒绝运行、不回退其他数据库、不输出连接异常中的模拟密码；连接失败使用mock，不是真实数据库结果。

## 准备的入口

run_postgres_locks.py复用run_table_boundary.load_writer的原服务源码与Django ORM。计划运行两组竞争：写入持锁时清空等待，以及清空持锁时写入等待。第一个操作进入远端替身前暂停；查询pg_stat_activity要求第二个操作处于Lock等待，之后释放第一个操作，要求第二个操作拒绝旧版本且没有远端调用。

连接固定到127.0.0.1、专用数据库及用户agent_lab_test，默认端口55432，须显式设置AGENT_LAB_PG_ENABLED=1和AGENT_LAB_PG_PASSWORD。每次新建随机UUID schema，Django连接的search_path只含该schema，完成后清理该schema。设置连接、锁、语句及线程等待超时；缺配置或连接失败不可静默降级。进程被强制终止可能留下schema，需按manifest中的随机schema核查清理。

bootstrap.setup增加显式database参数，默认仍为原内存SQLite；已配置Django的进程不能再用该参数切换数据库，集成入口必须独立进程启动。未修改生产后端架构。

## 尚未验证

真实PostgreSQL路径，包括模型建表、行锁等待、跨连接可见性和schema清理，目前都未执行。远端仍为独立SQLite替身，IAM与审核为允许夹具，即使未来两组通过，也只支持PostgreSQL锁链路结论，不能代表真实immudb或平台权限验证。没有新增真实模型成绩。

下一步需配置本机专用测试库并运行该入口，再接入隔离immudb；不使用现有生产数据库替代测试环境。
