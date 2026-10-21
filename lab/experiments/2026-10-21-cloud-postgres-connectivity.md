# 云测试库连接预检

用户提供腾讯云PostgreSQL测试库并授权运行。已将连接信息保存在忽略文件lab/.postgres.local.json，未提交密码或连接配置。

测试入口新增--config显式JSON输入，支持主机、端口、数据库和用户；强制sslmode=require，仍只允许专用agent_lab_test或test_开头的数据库名。TLS require提供传输加密要求，不等同于证书主机名校验。没有禁用TLS来规避连接问题。

实际运行输出：lab/results/tencent-postgres-lock-smoke-2/summary.json。结果为unavailable、checks_passed=0、postgresql_lock_behavior_tested=false。随后独立TCP及PostgreSQL连接诊断均超时；域名可解析，未进入账号鉴权，无法判断凭据是否有效。没有创建测试schema或改动数据库数据。

当前待检查外网访问、白名单/安全组或客户端网络规则；超时本身不能确定具体原因。网络放通后复用入口运行真实行锁场景，不把本次连接尝试记作集成测试通过。

本地入口检查4项通过，新增一项覆盖显式远程配置、TLS要求及拒绝非测试库名；这些均为配置合同检查，不是真实云数据库行为验证。原并发测试仍使用SQLite远端替身，即便PostgreSQL连接成功，也不等于真实immudb集成。
