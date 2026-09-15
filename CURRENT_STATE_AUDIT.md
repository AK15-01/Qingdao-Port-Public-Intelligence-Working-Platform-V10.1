<!-- portscope-state-audit:{"database_sha256":"5fbcec36ee4e563b4f853fae2c1386627a9887c64a9cb13cecb7229e9ec68d7f","code_version":"0.5.1-beta","generated_at":"2026-09-15T18:37:21+10:00"} -->
# PortScope 当前真实状态审计

> 本文件由当前 SQLite 动态生成。数据库哈希或代码版本变化后，本文件即为过期快照，必须重新生成。

- 生成时间：2026-09-15T18:37:21+10:00
- 代码版本：`0.5.1-beta`
- 数据库：`data/portscope.db`
- 数据库 SHA-256：`5fbcec36ee4e563b4f853fae2c1386627a9887c64a9cb13cecb7229e9ec68d7f`

## 数据状态

| 项目 | 总数 | active | quarantined |
|---|---:|---:|---:|
| 文档 | 45 | 9 | 36 |
| 事件 | 13 | 9 | 4 |

- active 文档中 AI 已完成：9
- active 事件证据：已验证 21，未解决 0，共 21
- 独立真人审核事件：0
- 内部使用资格：0
- 客户报告资格：0
- 历史报告记录：2

## 任务状态

| operation_runs 状态 | 数量 |
|---|---:|
| failed | 4 |
| partially_succeeded | 1 |
| succeeded | 22 |

| crawl_source_runs 状态 | 数量 |
|---|---:|
| failed | 1 |
| partially_succeeded | 1 |
| succeeded | 8 |

## 启用来源

- `SRC-2FF28D3D7FC5` 山东海事局海上风险预警｜健康：正常｜最近成功：2026-07-29T20:58:35+10:00

## 最新成功采集

`CRAWL-8122A5661768`，2026-07-29T20:58:35+10:00，新增文档 0，新增事件 0

## 口径说明

- `active`不等于客户报告可用；客户报告资格继续受真人审核、证据和来源规则约束。
- 本审计不把自动或代理标注计为独立真人审核。
- QA工作区的5条未定位证据不因本文件生成而改变。
