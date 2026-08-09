# 分支收敛决策

## 唯一主版本

本轮唯一主版本为桌面目录 `青岛港公开情报试验台`。带 `Copy` 或 `Copy (2)` 后缀的目录只作为历史只读参考，不进行双线开发，也不以历史目录覆盖主版本。

## 已吸收的通用能力

- 将历史 `test_commercial_hardening.py` 所表达的“未独立真人核验不得计入人工准确率”“低价值内容不得进入核心报告”“许可门禁不得被内部流程绕开”等通用要求，纳入主版本现有测试与本轮新增的核验溯源、证据绑定和受控晋升测试。
- 参考 `qa/generate_acceptance_report.py` 的“自动指标与人工指标分开”原则，继续在主版本更成熟的 `qa/evaluate_real_acceptance.py` 上实现，不复制旧脚本。
- 参考 `qa/post_process_acceptance.py` 的验收后处理目的，复用主版本现有 QA 流水线、RAG 与报告模块；新增的是独立、门禁化的逐事件晋升，而不是整库复制。

## 未合并的文件或行为

- 未直接合并上述三个历史文件：主版本已有覆盖面更广、结构更新的实现，直接复制会恢复硬编码模型、旧字段和重复逻辑。
- 未合并 `.workbuddy`、WorkBuddy 专用 `conftest` 删除保护、微信公众号跨域自动抓取、临时数据库、真实 QA 输出、`.env`、缓存、日志和报告文件。
- 未合并任何绕过白名单、robots、TLS、登录或访问控制的逻辑。

## 对应验证

- 人工来源：`review_provenance.py`、`event_reviews`、`qa/evaluate_real_acceptance.py`。
- 证据逐字绑定：`evidence_binding.py`、`event_evidence`、报告和引用门禁测试。
- QA 晋升：`qa_promotion.py`，按事件事务写入并记录 `promotion_history` 与操作历史。
- 报告与低价值门禁：`commercial_report.py`、`platform_report.py`。
- 发行隔离：`package_release.py`、`security_audit.py`。

结论：历史分支没有任何文件级“全量合并”；仅吸收可验证、与当前架构一致的通用约束。
