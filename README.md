# 中医适宜技术体验安全记录服务

本项目在中医文化夜市通用后台基础能力（活动机构、服务站点、操作者、结构化资料、角色权限、
请求幂等、SQLite 事务与哈希串联审计）之上，实现适宜技术体验（如耳穴压豆）的安全记录闭环。

## 安全规则

- **开项四验**：开始操作前在同一事务内同时核验
  1. 不可变的项目规程版本（版本内容哈希固化，发布后禁止修改/删除）；
  2. 执行人员资格（归属人、项目、有效期、撤销状态）；
  3. 参与者筛查结论（站点、项目、参与者一致，结论适宜且在有效期内）；
  4. 器材领用关系（领用未归还、领用人即执行人）与器材批次暂停状态。

  任一条件失效都返回 `412 precondition_failed`，不能进入执行状态。
- **事实只追加**：操作开始后的观察、不适与系统事实以哈希链逐条追加，
  SQLite 触发器物理禁止对 `session_facts`、`procedure_versions`、`incidents`、
  `incident_affected` 的 UPDATE/DELETE；已结项复核与暂停边界同样不可改写。
- **异常原子处置**：异常事件在一个事务内
  封存本次过程 → 封停同批器材 → 挂起同批其他在施操作 → 生成逐项待复核清单。
  事件号 + 事实摘要双重约束：同一事件重放返回原结果（不扩大停用范围），
  事件号相同而事实不同则拒绝；传输层 `request_id` 同样幂等。
- **逐项复核解除**：暂停只能由复核人员（reviewer/admin）逐项关联处置证据后解除，
  无证据不得结项；清单全部处置完才解除批次暂停。
  迟到的正常完成回执只作为 `completion_receipt_late` 事实留存，不能越过封存/暂停决定。
- **重启不丢边界**：全部状态落在 SQLite，进程重启后暂停边界与未完成复核原样保留。

## 目录

- `src/night_market_foundation/`：基础登记服务（`service.py`）、安全记录服务（`safety.py`）、
  SQLite 建表与不可变触发器（`storage.py`）、哈希审计链（`audit.py`）、HTTP 路由（`api.py`）、
  离线验收（`acceptance.py`）。
- `tests/`：基础规则、安全闭环、HTTP 路由与端到端验收测试。

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

验收命令在临时 SQLite 数据库中完整走查：建档与四验拦截 → 开项与事实追加 →
异常封存与同批封停 → 事件重放/异文拒绝 → 迟到回执 → 两次进程重启后的边界保留 →
逐项关联证据解除暂停 → 全链追溯，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写接口要求
`request_id` 幂等键，重放返回 `200`、新建返回 `201`。

### 安全记录接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /procedures` | 发布不可变规程版本 |
| `POST /qualifications`、`POST /qualifications/revoke` | 登记/撤销人员资格及有效期限 |
| `POST /screenings` | 登记参与者对某项目的筛查结论 |
| `POST /equipment-batches`、`POST /equipment-loans`、`POST /equipment-returns` | 器材批次、领用与归还 |
| `POST /safety-sessions/start` | 四验通过后开项 |
| `POST /safety-sessions/facts` | 追加观察/不适事实 |
| `POST /safety-sessions/complete` | 正常完成回执（迟到则不生效） |
| `POST /incidents` | 异常事件：原子封存、同批封停、生成复核清单 |
| `POST /review-items/resolve` | 复核员逐项关联处置证据 |
| `GET /review-items`、`GET /suspensions`、`GET /incidents/{no}` | 清单、暂停边界与事件查询 |
| `GET /safety-sessions/{id}/trace` | 从一次体验追溯规程、人员、器材、事实链与全部风险处置 |

服务重启后 SQLite 中的业务状态、暂停边界、未完成复核与审计链继续保留。
