# 实现中医适宜技术体验安全闭环基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/experience_safety/`：适宜技术体验安全记录模块（准入门禁、追加事实、异常封存、复核放行与追溯）；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 适宜技术体验安全记录

`experience_safety` 模块在基础层之上实现耳穴等适宜技术体验的安全闭环：

- **准入门禁**：`start_session` 在同一事务内同时核验不可变的规程版本、执行人员资格期限、参与者最新筛查结论和器材领用关系，任一条件失效都不能进入执行状态；命中安全暂停的项目规程或器材批次同样禁止开始；
- **只追加事实**：体验事实通过触发器在数据库层禁止更新与删除，正常回执（`complete_session`）不能越过已经生效的安全封存；
- **异常事件**：`report_incident` 以事件号为幂等键，原子地封存本次及同范围执行中的过程、对项目规程版本与器材批次建立或复用安全暂停（重复上报不扩大停用范围）、生成待复核清单；同一事件重放保持原结果，事件号相同而事实不同则拒绝；
- **复核放行**：复核人员须为每项待复核事项关联处置证据（`resolve_review_item`），全部完成后才能解除暂停（`lift_suspension`）；
- **追溯与重启**：`GET /safety/sessions/trace?session_id=...` 从任一次体验追溯规程、人员、器材和风险处置；全部状态存于 SQLite，进程重启不丢失暂停边界及未完成复核。

### 安全记录离线验收

```bash
PYTHONPATH=src python3 -m experience_safety.acceptance
```

验收覆盖门禁、封存、幂等重放、冲突拒绝、迟到回执拦截、重启保留和复核放行，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

### 安全记录 HTTP 服务

```bash
PYTHONPATH=src python3 -m experience_safety.api --database safety.sqlite3 --host 127.0.0.1 --port 8081
```

主要接口：`POST /safety/protocols|practitioners|participants|screenings|equipment|checkouts|sessions|incidents`、`POST /safety/sessions/facts|completion`、`POST /safety/reviews/resolutions`、`POST /safety/suspensions/lift`，以及 `GET /safety/sessions|sessions/facts|sessions/trace|suspensions|review-items`；基础层接口保持可用。
