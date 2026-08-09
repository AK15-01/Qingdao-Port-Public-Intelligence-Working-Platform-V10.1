# PortScope｜青岛港公开情报工作台

PortScope 是供项目所有者本人使用的单机、本地优先公开情报工作台。它用于采集、整理、检索和人工核验公开资料，再输出保留来源与证据的内部简报或客户分析；它不是第三方原文数据库销售系统。

> 当前版本：`0.5.0-beta`。适合在明确责任人、低频采集和逐项复核条件下进行受控内部试运营；尚未完成独立真人审核、真实客户试点或无人值守客户交付，也尚未取得任何外部来源的商业再利用授权。当前真正通过小规模稳定验收的有效来源主要来自山东海事局，其他来源仍受页面结构、TLS、JavaScript、许可或可用性限制。压缩包名称中的“V2.0”不代表产品已达到正式`2.0.0`。

1. “更新最近7天青岛港相关公开信息”；
2. “这些信息里最重要的风险是什么？”；
3. “生成一份面向货代公司的周报”。

系统不是青岛港、海事、气象、政府或交易机构的官方系统。风险与商机分数只用于公开信息排序，不是官方等级，不构成准确预测、法律意见、投资意见或操作指令。`data/sample_events.csv` 中全部是醒目标记的“虚构演示”，不会自动进入 SQLite 或真实报告。

## 默认日常工作台

- 工作台：首页只显示今日指标、八个快捷操作、最多三个当前会话任务和真正需要处理的待办；
- 采集与导入、文档库、事件库、人工审核、证据修复、智能问答、报告中心：分别完成一个主要任务；
- 商用准备：只用真实运行、真人审核、证据、交付与恢复记录判断“未达到 / 试点可用 / 正式交付可用”；
- 历史记录：持久化查询采集、导入、AI补跑、审核和报告任务；关闭首页卡片不删除业务历史；
- 来源与系统设置：显示来源状态与数据库、AI、正文、证据、审核、报告、备份和任务健康检查。

“高级工具”保留旧版AI工作台、草稿/CSV事件、规则、迁移、来源选择器、请求频率、AI日志和版本差异。日常页面没有绕开SSRF、robots、正文质量、生命周期、证据和人工审核门禁。

### 首次启动三步

1. 在AI工作台顶部粘贴DeepSeek API Key并测试；也可以跳过，继续使用本地规则和关键词检索；
2. 勾选合规确认并点击“一键初始化推荐公开来源”；
3. 在聊天框输入第一条任务，例如“更新最近7天公开数据并告诉我有什么值得关注”。

首次运行会自动创建一个本地默认工作空间。对话、来源、文档、审核、报告和确认队列始终按 `workspace_id` 隔离。

### 第一次更新公开数据

新工作空间不会再自动写入不可用的空白来源占位。如果当前没有可运行来源，首页和“更新公开数据”会显示“尚未启用公开数据源”：

1. 查看将要安装的官方来源名称和模板状态；
2. 勾选低频公开信息核对确认；
3. 点击“一键初始化推荐来源”；
4. 页面分别显示已启用、保持停用和“模板配置不完整”的来源；
5. 至少一个完整来源启用后即可点击“立即更新公开数据”。首页按钮执行“快速更新”：最多 3 篇、只抓新增、AI 默认关闭。

普通模式只展示来源名称、当前状态、启用/停用和最后更新时间。域名、列表页、CSS/URL 规则、robots、频率和许可仅在“专业设置”中展示。配置不完整或 robots 未确认允许的模板不能在普通模式启用，也不会阻塞其他完整来源。

### 既有来源模板升级与 PDF 附件返工

`config/default_sources.json` 的推荐来源带有 `template_version` 和 `template_updated_at`。已有同名来源不会再被整体跳过：系统只递归补充缺失的模板解析键，现有启停状态、采集许可、限速、用户备注、许可判断、报告/全文/原始数据权限和用户已经填写的适配器值都不会被模板覆盖。同步前使用 SQLite 在线备份；默认命令是只读预览，重复应用不会产生重复修改。

```powershell
# 只读预览；输出新增、保持和冲突字段
.\.venv\Scripts\python.exe scripts\sync_source_templates.py `
  --db data\portscope.db --degrade-zero-yield-sources

# 确认后应用；有真实样本但合格正文为0的山东港口来源会暂停自动采集
.\.venv\Scripts\python.exe scripts\sync_source_templates.py `
  --db data\portscope.db --degrade-zero-yield-sources --apply

# 只预览PDF附件名型隔离文档；不会访问列表页
.\.venv\Scripts\python.exe scripts\rework_pdf_attachments.py --limit 2

# 受控返工既有正文页及其同白名单域名PDF，保留旧错误版本
.\.venv\Scripts\python.exe scripts\rework_pdf_attachments.py `
  --limit 2 --apply --confirm PDF_REWORK
```

PDF 返工仅选择当前隔离、HTML 正文只有 PDF 附件名的记录。成功时保留正文页为 `canonical_url`、PDF 为 `source_file_url`，记录 PDF 哈希、大小、页数和新文档版本；失败时保留隔离状态和明确原因，不会伪造正文、人工审核或报告资格。

对因官方固定模板相似度而隔离、但最新dry-run已通过结构化事实门禁的预警PDF，文档库提供“官方预警PDF模板复评”折叠区。只有用户逐条或批量勾选并确认后才转入`待AI处理/待人工审核`；原隔离原因和复评记录保留。该操作不生成真人审核，不自动通过证据，也不赋予客户报告资格。

## 自动更新流程

```text
启用的白名单来源
→ 白名单、robots 与访问控制门禁
→ API / RSS / HTML 列表页增量发现
→ 白名单正文获取与安全重定向检查
→ HTML 清洗和正文提取，或官方文本型 PDF 安全解析
→ canonical URL 与 content_hash 去重
→ 原始 HTML 归档和文档版本链
→ 本地规则事件与透明风险/商机评分
→ 中文段落分块与 SQLite FTS5
→ 待 AI 处理 / 待人工审核
```

默认采集不会逐篇调用 DeepSeek，也不会同步构建 Chroma；用户可在采集完成后分别创建“AI补跑”和“向量索引”任务。高级参数可明确开启采集后 AI，但默认关闭并显示最大调用量。

一个来源失败不会中断其他来源。新 URL 执行完整处理；既有 URL 默认直接跳过。低频复查最近 7 天时，系统优先发送 `If-None-Match` / `If-Modified-Since`，HTTP 304 只更新检查时间，不下载 PDF、不调用 AI、不创建事件或索引；站点没有条件请求标识时，24 小时内不重复检查，超过 30 天且已有完整文件哈希的内容默认跳过。只有用户明确打开“强制重新抓取”时才完整复查历史内容。URL 相同但正文变化时创建新 `document_version`，保留旧版、`previous_document_id` 和原始文件，不覆盖历史。

快速更新最多 3 篇，标准更新最多 5 篇，自定义更新允许 1—20 篇；历史补采和强制刷新均需明确选择。单一来源默认总预算 120 秒，列表/HTML/PDF 请求使用有上限的超时，Worker 最多重试 1 次且继续保留合规请求间隔。

采集、AI补跑和索引使用 SQLite `operation_runs` 持久化任务与独立本地 Worker。页面只轮询任务状态，刷新不会重复执行；同一工作空间同类任务同时最多一个。任务逐篇保存 `heartbeat_at`、当前阶段/文章、成功/跳过/隔离/失败计数。取消按钮仅设置 `cancel_requested`，Worker 在来源、文章、HTML/PDF、AI文档或索引文档边界安全停止，已提交结果仍可立即查询。

点击“完成并关闭”只隐藏首页卡片并写入归档时间，任务原始结果（成功、部分成功、失败或取消）不会被改写。失败、部分成功和取消任务才能重试；成功任务即使已关闭也不会被错误当作失败任务。每个Worker的stdout/stderr写入`output/task_logs/<任务ID>.log`，领取任务前立即退出会在短时间内标记失败；queued默认3分钟恢复，running继续按30分钟心跳恢复。任务日志可在历史页查看，路径受限于任务日志目录，错误摘要和页面输出会脱敏。

`CURRENT_STATE_AUDIT.md`由当前SQLite动态生成，不是手工结论：

```bat
.venv\Scripts\python.exe scripts\generate_current_state_audit.py
```

文件记录生成时间、数据库SHA-256和`VERSION`。数据库或代码版本变化后，快照检查会标记为过期。

测试临时数据库、缓存和过期调试输出使用只读预览后再清理：

```bat
.venv\Scripts\python.exe scripts\cleanup_artifacts.py --dry-run
.venv\Scripts\python.exe scripts\cleanup_artifacts.py --apply --confirm CLEAN_ARTIFACTS
```

默认保护`.venv`、正式数据库、原始业务文档、`output/qa_real`、QA标签、证据、业务报告、备份及最新任务日志；未提供二次确认词不会删除任何文件。

每个 `crawl_run` 还记录数据源检查、列表发现、正文抓取、清洗、去重、归档、抽取/回退、评分、Chunk、FTS5、向量索引、待审核和报告候选各阶段的输入/成功/失败/跳过、失败原因和耗时，以及API调用次数、输入/输出字符数、实际模型和未换算金额的调用量说明。DeepSeek不可用时不会重新抓网页：文档进入“待AI处理”，可在AI工作台输入“把所有待AI处理文档重新处理一遍”进行补跑。

## SQLite 核心存储

核心数据库为 `data/portscope.db`。主要表：

- `sources`：白名单域名、适配器、robots、访问条款、内部处理/客户摘要/全文再分发/原始数据转售的分离门禁、限速和游标状态；
- `crawl_runs`：每次运行的发现、获取、跳过、失败、新增和更新统计；
- `documents`：URL、标题、发布/获取时间、正文、哈希、原始 HTML、版本链和当前版本；
- `events`：结构化事件、抽取方式/置信度、人工确认、报告资格和透明评分；
- `document_chunks`：文本块、字符数、元数据和向量化状态；
- `reports`：报告周期、事件/文档清单、版本和三个文件路径；
- `report_delivery_snapshots` / `report_delivery_amendments`：客户报告事件版本、证据、审核人与文件哈希的不可变交付快照及交付状态历史；
- `restore_drills`：恢复到隔离测试目录后的SQLite完整性、数量核对和演练报告；
- `customer_feedback`：仅由用户手工录入的真实试点反馈，不允许AI生成；
- `qa_logs`：问题、答案、真实文档 ID 和引用 JSON；
- `ai_call_logs`：模型、输入/输出字符数、耗时、成功状态和错误类型，不记录密钥；
- `agent_conversations` / `agent_messages`：按工作空间隔离的对话与安全结果卡片；
- `agent_approvals`：网络、来源变更、索引重建和批量审核的确认门禁。
- `event_evidence`：逐条引用与当前文档版本、哈希和原文字符偏移的绑定；
- `event_reviews`：核验人类型、姓名、时间、方法、版本、逐项勾选和决定；
- `promotion_history`：QA 数据逐事件晋升至正式工作空间的来源、运行ID和审计链。
- `operation_runs`：统一的业务任务生命周期、输入/结果摘要、计数和关联业务对象；
- `technical_logs`：与业务历史分离、可按保留期清理的技术错误与重试详情。

工作空间、客户、操作历史和报告版本继续使用同一 SQLite。不同工作空间通过 `workspace_id` 隔离。旧版工作空间 CSV 不会被自动删除。

### CSV 迁移与导出

专业模式的旧版设置入口可迁移 `events.csv`、`sources.csv` 和 `intake.csv`。迁移前先复制到 `data/legacy_backup/<时间>/`，再用 SQLite 事务导入。旧事件会生成可追溯的迁移文档记录，并默认要求重新核对许可。

专业设置可将 SQLite 中的事件、来源和文档元数据导出为 CSV。CSV 继续用于交换和备份，但不再是自动采集、版本管理和 RAG 的核心状态。

## 白名单、robots 与商业许可

系统只访问当前工作空间中同时满足 `enabled=1` 和 `crawl_allowed=1` 的来源，而且每次请求和重定向都必须仍属于该来源的白名单域名。

`config/default_sources.json` 保存经过入口核对的推荐模板，不填公告、不虚构事件：

- 山东海事局海上风险预警；
- 青岛市海洋预报暨海洋预警信息发布平台；
- 山东省港口集团要闻；
- 青岛市交通运输局综合运输管理。

2026-07-23 的分阶段真实核对结果见文末“当前真实来源限制”：只有山东海事局风险预警 PDF 栏目和政务动态 HTML 栏目通过阶段 A/B；JavaScript 空壳、DOC/PPTX 附件、TLS 失败和超时来源均已暂停。许可状态不明确不会阻止内部工作台处理公开信息；网页公开可访问仍不等于允许批量全文再分发或出售原始数据库。

已实现的适配器：

- `api`：公开 JSON API；
- `rss`：RSS/Atom；
- `generic_html`：配置驱动的静态 HTML 列表页；
- `shandong_maritime`、`qingdao_ocean`、`qingdao_government`：命名的官方栏目适配器。它们仍要求在专业设置中填写可审计的链接选择器、允许路径和 URL 正则，不会猜测网站结构。

列表适配器支持文章链接、下一页、标题、日期、正文、发布机构、排除元素、允许路径和 URL 正则配置。选择器解析失败会记录错误、保留已取得的页面，不创建虚构正文，也不阻塞其他来源。

## 网络安全与礼貌访问

- 只允许 HTTP/HTTPS；
- 拒绝 localhost、回环、`0.0.0.0`、私有/链路本地/保留 IP 和云元数据地址；
- DNS 解析前检查，重定向后重新检查安全地址和白名单域名；
- 不保存 Cookie、账号或登录凭据；
- 不登录、不绕过验证码、付费墙、robots 或访问控制；
- 使用可识别的 `PortScope` User-Agent，不伪装搜索引擎；
- 每域名请求间隔至少 2 秒，可配置得更慢；
- 限制 timeout、重定向、响应体、列表页数和文章数；
- 网络失败重试采用指数退避；
- 不自动定时运行，不扫描站点地图，不使用浏览器自动化。

所有 pytest 网络测试使用 Mock/fixture，不真实访问任何官方站点。

## 文档、AI 抽取与人工审核

清洗器移除 `script`、`style`、`nav`、`footer` 等明显无关内容，合并空白并限制正文长度。原始 HTML 按 `data/raw/YYYY/MM/DD/<document_id>.html` 保存，文件名不用网页标题。

DeepSeek 只处理公开正文，输出由 Pydantic 严格验证，字段包括标题、日期、类别、事实摘要、潜在影响、区域、时段、状态、风险/商机词、建议动作、关联关键词、置信度和证据片段。原始 URL、真实发布日期、发布机构、获取时间、正文和 `content_hash` 始终来自可信元数据，AI 不能修改。

网页正文是不可信输入。系统提示明确要求模型把网页中的命令、角色提示、链接要求和“忽略此前指令”全部当普通文本；不得访问正文链接、读取本地文件、泄露提示词或密钥，也不得在检索证据之外补充具体青岛港事实。

未配置密钥或 API 失败时：

- 采集继续；
- 文档和原文继续入库；
- 本地规则继续分类、评分和分块；
- 文档保持待 AI/人工处理；
- FTS5 问答仍可使用原文；
- 应用不崩溃。

智能问答页会单独显示 DeepSeek API、知识库文档和向量索引三个状态。没有 API 时可继续使用关键词检索；没有文档时提示先更新公开数据；有文档但没有索引时显示“构建知识库”按钮。

自动事件默认 `human_verified=0`、`report_eligible=0`。高度疑似重复、解除未关联、数据校验错误、逐字证据失败，以及来源明确禁止必要摘要/短引会阻止进入客户报告。许可未明确会显示风险提示但不阻止内部分析。批量确认仍要求人工逐条核对原始来源；AI 不会自动确认、自动关闭风险或覆盖确定性分数。

## DeepSeek 配置

AI默认关闭。普通用户直接在“AI工作台 → 配置DeepSeek API”或“设置”页面填写，无需手工创建 `.env`：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_AGENT_MODEL=deepseek-v4-pro
DEEPSEEK_EXTRACTION_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=60
DEEPSEEK_MAX_RETRIES=2
```

页面用密码输入框接收密钥，通过同目录临时文件和原子替换只写入项目根目录 `.env`；保存后下一次读取立即生效，不需要重启。密钥不写入SQLite、对话、报告或日志，页面只显示 `sk-****1234`。清除按钮会原子清空密钥并关闭当前工作空间AI开关。`.env` 已被 `.gitignore` 和交付打包脚本排除。

Agent规划默认使用 `deepseek-v4-pro`，批量结构化抽取默认使用 `deepseek-v4-flash`，两者可分别修改。启用AI前只可发送公开信息，不得发送个人信息、客户内部数据或非公开材料。

“快速测试”只执行 `GET /models` 和一次 `POST /chat/completions` 极短生成；余额改为单独的“查询余额”按钮，避免每次诊断累积不必要请求。400、401、402、403、404、422、429、5xx、超时、DNS、TLS和代理错误分别解释；提供商错误只显示脱敏后的 `error.code` 和限长 `error.message`。

模型不再由Python白名单限制。页面使用 `/models` 实时结果，并把不含密钥的模型列表缓存到 `data/model_cache/deepseek_models.json` 24小时；离线时可使用最近缓存，也允许手工输入新模型ID。`auto-agent` 优先选择高能力模型，`auto-extraction` 优先选择快速低成本模型；旧 `deepseek-chat` / `deepseek-reasoner` 配置会提示迁移，不会让应用崩溃。每次真实调用记录实际 `model_id`，报告如未调用AI则明确记录 `deterministic-rules`。

桌面模式正常优先读取项目根目录 `.env`，然后才读取 `PORTSCOPE_ENV_PATH` 指定文件、操作系统环境变量和默认值；测试或受控部署显式传入的配置文件优先。文件中的空Key也是权威值，因此页面清除后不会被Windows遗留环境变量“复活”。页面始终显示Key、模型和Base URL来源以及相对配置路径，Key存在不等于连接已验证，Key、连接诊断和工作空间 `ai_enabled` 分开显示。

## 受控Agent工具调用

DeepSeek使用官方OpenAI兼容Tool Calls格式。真实API传输使用SSE流式接收可见回答和工具参数片段，业务工具进度同步显示；模型只能从注册表选择工具，不能执行任意Python、Shell、SQL或文件路径。所有参数先经Pydantic `extra=forbid` 校验；未注册工具直接拒绝。思考模式发生工具调用时，程序在后续API请求中完整回传该assistant消息的 `reasoning_content`，但不在页面显示隐藏推理。

内置27个工具：`get_system_status`、`configure_ai`、`test_ai_connection`、`list_sources`、`initialize_recommended_sources`、`analyze_source_url`、`propose_source_adapter`、`add_source`、`enable_source`、`disable_source`、`run_crawl`、`get_crawl_progress`、`get_crawl_result`、`retry_failed_sources`、`search_documents`、`ask_knowledge_base`、`rebuild_index`、`list_pending_reviews`、`approve_events`、`reject_events`、`analyze_risks`、`analyze_opportunities`、`compare_periods`、`generate_report`、`list_reports`、`export_data` 和 `explain_error`。

多轮工具调用最多 4 轮、总预算 90 秒、最多 8 次工具调用；相同工具与相同参数重复第 2 次，或连续工具调用没有新增结果时立即停止。网络采集、单页来源分析、来源增删启停、索引重建和批量审核必须显示操作范围并由用户确认。采集、AI补跑和索引工具只创建可恢复的持久化任务，不在聊天请求内同步阻塞。报告工具只生成可下载草稿；正式发布仍需人工确认。

自动来源助手只读取用户提供的一张栏目页、发现最多5个候选链接并测试最多一篇正文，然后建议CSS选择器、允许路径和URL正则。SSRF、DNS、白名单、robots与许可仍由确定性程序判断，AI不能自行启用来源。

## 本地知识库与 RAG

正文默认按中文段落和 500–800 字符分块，重叠约 100 字符，Chunk 保留文档、事件、来源、日期、类别、状态、原文 URL、人工确认和报告资格元数据。

- 关键词索引：SQLite FTS5，并提供中文子串回退；
- 本地 Embedding：`BAAI/bge-small-zh-v1.5`；
- 向量库：Chroma `PersistentClient`，目录 `data/chroma/`；
- Collection：`port_documents`（代码也支持独立 `port_events` collection）；
- 更新文档：删除旧文档有效向量后写入新版本；
- 重建：运行 `rebuild_index.bat` 或在专业设置点击“重建知识库索引”。

第一次真正向量化时会显示/打印模型下载提示。模型下载完成后 Embedding 在本地生成，正文不会为了向量化发送给 DeepSeek。若模型或 Chroma 暂不可用，Chunk 保持“待向量化”，FTS5 检索仍可工作。

混合检索融合关键词与向量结果，并支持日期、来源、类别、状态、人工确认和商业报告资格过滤。排序优先考虑当前版本、来源、日期、人工确认和相关性。

风险/商机查询还可限定为高、中业务价值，避免党建、荣誉和普通活动稿进入核心回答。若本地 BGE 模型尚未完整缓存或下载网络不可用，FTS5 仍可使用，但应把语义向量能力视为受限，不能把关键词回退表述为完整语义检索。

问答中的每条引用都必须映射到当前 SQLite 中真实存在且为当前版本的 `document_id`。来源卡片显示标题、发布机构、发布日期、原文链接、引用片段和获取时间。模型如果返回未检索到的文档 ID，会被拒绝并回退到本地证据整理；完全无证据时固定回答：

> 当前知识库中没有足够的公开来源支持这一结论。

## 报告中心：内部研究与客户交付

报告中心明确提供两种模式：

- **内部研究版**：允许纳入原始公开来源和有效URL存在、非明显重复、但商业再利用许可尚待确认的资料；封面、页眉、HTML和Excel均标记“仅供内部研究”，不全文转载网页，不提供可作为原始数据再销售的附件，并逐条显示许可待核对状态。
- **客户交付版**：严格要求独立真人核验、当前版本证据逐字可定位、来源与日期有效、非高度疑似重复，并且来源未明确禁止必要事实摘要和短引用。许可状态未明确时报告显示风险提示，不附第三方全文；明确禁止摘要/短引时才拦截。没有合格数据时会阻止生成空洞报告。

### 商用准备门禁与交付快照

“商用准备”页面不会把代码测试通过当作商用就绪。它从SQLite实时计算连续真实运行天数、来源成功率、合格正文率、事件产出率、真人审核数量、核心事件真人复核率、逐字证据率、真人修改前后关键字段准确率、重复率、按时交付率、备份与恢复演练时间，以及客户报告合格事件数。真人样本不足50条时，字段准确率明确显示“样本不足”。

客户交付版在生成前执行统一预检：真人审核、当前文档版本的逐字证据、驳回/待二审、过期预警、重复、关键字段、内部路径或数据库ID、长篇第三方原文、来源使用风险和事实/推断混写。阻断项存在时只能生成内部研究草稿。预检通过后，系统保存事件和文档版本、来源URL、证据与哈希、核验人、三份文件SHA-256、数据截止时间和报告版本；快照内容不能覆盖，实际交付时间只通过独立状态历史记录。

恢复演练从现有备份恢复到 `output/restore_drills/<演练ID>/restored/portscope.db`，不会覆盖生产数据库。系统执行SQLite完整性检查并核对文档、事件、审核、证据和任务数量；只有真实成功才更新最近恢复演练时间。客户试点反馈必须由用户勾选真实性确认后手工录入，系统和AI不会自动生成。

来源合规清单分别记录公开访问、访问频率、个人信息/重要数据风险、内部采集与分析、客户摘要、必要短引用、全文再分发和原始数据转售。未经明确依据和复核日期，全文再分发及原始数据转售保持关闭。
- **原始数据导出**：与客户简报完全分离。第三方全文、批量网页快照和原始数据库导出默认禁止，只有来源明确允许且用户二次确认时才可执行。

客户交付版只纳入以下内容：

- 来源有效，且未明确禁止必要事实摘要和短引用；
- 当前文档版本且正文提取成功；
- 非高度疑似重复；
- 通过数据校验；
- `human_verified=1`；
- `report_eligible=1`。

DOCX/HTML 包含封面、客户和周期、执行摘要、本期关键变化、当前有效风险、海事和天气、港口及企业动态、政策和采购商机、已解除事项、建议关注、数据质量、来源附录、方法与免责声明。每条核心事件明确显示发生了什么、来源/日期、影响对象、可能影响的业务环节、紧急程度、当前状态、标为“分析”的潜在影响与核对动作、证据片段、AI置信度和人工确认状态。事实卡片带来源编号，附录保留可点击原文链接。DOCX 使用正式标题层级、中文字体回退、固定表格几何、页眉页脚和页码。

客户交付版Excel附件包含7个工作表：事件明细、当前风险、已解除风险、商机清单、来源清单、数据质量、抓取运行记录。内部研究版增加“内部使用说明”，并明确附件不含网页原文、不得作为原始数据再销售。

同一客户、标题和周期再次生成时版本号递增，文件名不覆盖上一版；SQLite 记录事件/文档清单和 DOCX/HTML/Excel 路径。本项目不强制生成 PDF，用户可用 Word 或浏览器导出 PDF，避免中文字体和系统组件不稳定。

## Windows 运行

当前项目继续使用项目目录中的 Python 3.9 虚拟环境：

```text
<项目目录>\.venv\Scripts\python.exe
```

日常启动不会读取依赖哈希、不会执行 `pip`、不会创建或升级虚拟环境，也不会调用 PATH、用户目录或其他项目中的 Python。

### 完整应用

直接双击 `run.bat`。也可以从任意 CMD 或 PowerShell 目录调用它，例如：

```bat
"<项目目录>\run.bat"
```

`run.bat` 使用自身的 `%~dp0` 定位项目，因此当前 CMD 目录不会影响 Python 和 `app.py` 的选择。启动前只快速检查项目标识、`app.py`、项目目录、核心模块、数据库和端口；核心模块包括 QA 人工标签所需的 `openpyxl`。通过后直接运行：

```bat
"<项目目录>\.venv\Scripts\python.exe" -m streamlit run "<项目目录>\app.py"
```

8501 已被当前 PortScope 占用时会显示现有地址并停止重复启动；被其他程序占用时会自动尝试 8502–8599。

### 只读检查和诊断

以下命令不启动服务、不联网、不调用 `pip`，退出码 0 表示核心启动条件通过：

```bat
run.bat --check-only
```

详细诊断会额外实际导入 ChromaDB、sentence-transformers 和 Torch，因此可能需要几十秒，但不会下载模型：

```bat
diagnose_environment.bat
```

诊断显示项目目录、实际解释器、Python/Streamlit版本、核心和高级模块、SQLite路径与完整性、8501端口、OneDrive状态和建议操作。高级RAG依赖异常只显示警告，不会让日常启动器安装依赖。

### 首次安装与主动修复

只有用户主动运行以下脚本时才可能调用 `pip`：

```bat
setup_environment.bat
repair_environment.bat
```

- `setup_environment.bat` 只用于 `.venv` 不存在时的首次Python 3.9安装。发现现有 `.venv` 会立即退出，不覆盖、不升级、不重建；只有完整安装成功后才写入依赖哈希。
- `repair_environment.bat` 先执行只读诊断，列出缺失核心包，用户输入 `REPAIR` 后才安装这些缺失包；默认不重装 `requirements-lock.txt` 中约117个包，并把真实输出保存到 `output\environment_repair\repair-latest.log`。
- `requirements-lock.txt` 仍保留用于明确的首次安装和版本验收，但已退出 `run.bat`、启动器和日常环境检查链路。锁文件或旧哈希变化不会阻止工作台启动。

若诊断明确提示缺少 `openpyxl`，请主动运行 `repair_environment.bat` 并输入 `REPAIR`。脚本只安装诊断列出的缺失核心包，不会重装整个锁文件。依赖缺失期间，首页会明确提示“QA Excel读取组件缺失”，而不会把读取失败误报为“没有QA数据”；其他不依赖QA Excel的工作台区域仍可使用。

直接运行依赖审计不会调用 `pip`：

```bat
.\.venv\Scripts\python.exe scripts\audit_runtime_dependencies.py
```

`update_data.bat` 和 `rebuild_index.bat` 仍调用 `ensure_environment.bat`，但该脚本现在只是无安装的兼容性检查器。

## Public Demo Deployment

公网展示通过统一的 `PUBLIC_DEMO_MODE` 开关启用。未设置时保持现有本地完整版；设为 `true` 后，`app.py` 只渲染只读 Demo 页面，不初始化正式工作空间，也不暴露采集、任意 URL、上传、AI 调用、报告生成、数据导出或管理操作。

公网 Demo 使用 `data/demo/portscope_demo.db`。这是由正式库中公开来源的合格记录生成的小型脱敏快照，只保留来源入口、事实摘要、结构化字段和必要短证据；不包含第三方全文、本地文件路径、任务日志、客户资料、对话、API日志或正式数据库。重新生成快照必须由项目所有者在本地明确执行：

```powershell
.\.venv\Scripts\python.exe scripts\build_public_demo_data.py
```

### 本地测试

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

公网模式的 PowerShell 启动示例：

```powershell
$env:PUBLIC_DEMO_MODE="true"
streamlit run app.py
```

Linux / Render 启动命令：

```bash
PUBLIC_DEMO_MODE=true streamlit run app.py --server.address 0.0.0.0 --server.port "${PORT:-8501}"
```

Streamlit Community Cloud 将入口文件设为 `app.py`，在 Advanced settings → Secrets 中配置：

```toml
PUBLIC_DEMO_MODE = true
DEEPSEEK_API_KEY = ""
```

Demo 展示不需要 DeepSeek Key。没有 Key 时历史演示数据正常显示，页面仅提示 AI 实时分析未启用。若部署环境确需提供 Key，只能使用平台环境变量或 Streamlit Secrets；公网模式不会读取项目根目录 `.env`，也不会提供网页端保存 Key 的入口。配置优先级为“环境变量 → Streamlit Secrets → 安全默认值”。

### 部署检查与安全检查

```bash
PUBLIC_DEMO_MODE=true python deployment_check.py
python scripts/scan_public_demo_safety.py
```

`deployment_check.py` 检查 Python、直接运行依赖、目录、只读演示库、配置和临时目录写权限。缺少 DeepSeek Key 不是启动失败。安全扫描只报告文件与问题类型，不输出疑似 Secret 内容。

仓库不得提交 `.env`、`.streamlit/secrets.toml`、正式 SQLite、原始网页/PDF、Chroma、日志、报告和历史 ZIP。`.env.example` 只能保留占位值。公网服务器使用 Linux 时不依赖任何 `.bat` 文件；Windows 的 `run.bat`、安装、修复和诊断能力继续保留给本地完整版。

推荐使用 Python 3.11 或平台当前支持的稳定版本；部署前必须运行全量测试和上述检查。当前最适合 Streamlit Community Cloud、Render 或能够运行标准 Python/Streamlit 命令并提供环境变量、持久化仓库文件的同类平台。公网 Demo 是只读展示，不依赖可写持久磁盘。

### OneDrive提示

当前项目位于OneDrive同步目录。启动器会给出非阻塞提示，不会自动移动项目。主平台SQLite连接设置了30秒超时；仍应避免两个实例同时写库，也不要直接复制正在写入的数据库作为备份。长期稳定运行建议把项目迁移到例如 `C:\PortScope\`，并使用项目自带的SQLite在线备份脚本迁移数据。

### 商用部署前检查

双击`commercial_preflight.bat`，或运行：

```powershell
.\.venv\Scripts\python.exe commercial_preflight.py
```

检查覆盖Python版本、锁定依赖、必要交付文件、密钥排除、SQLite完整性、FTS5、启用来源的白名单/robots配置，以及商业许可未确认却允许进入客户报告的错误配置。检查不会显示API Key，也不会修改数据库。

正式签约前，经营主体应把`config/commercial_profile.example.json`复制为`config/commercial_profile.json`，填写产品权利人、支持/隐私联系人、合同版本、适用法律和批准信息。项目不能代替经营主体猜测这些法律事实；未填写时检查会给出明确警告，`--strict`模式会阻止发布。完整签署项见`COMMERCIAL_DEPLOYMENT_CHECKLIST.md`，数据处理说明见`PRIVACY_AND_DATA_HANDLING.md`。

### 命令行更新

双击 `update_data.bat`，执行一次“采集 → 清洗 → 抽取/回退 → 评分 → 分块 → 向量化”。它不会启动浏览器，也不会定时运行。

### 重建索引

双击 `rebuild_index.bat`，根据 SQLite 当前版本文档重建 `data/chroma/`。

### 生成干净交付包

双击 `package_release.bat`，或运行：

```powershell
python package_release.py
```

交付包输出到 `dist/`，自动排除 `.venv`、`.git`、`.pytest_cache`、`__pycache__`、`.env`、正式SQLite、Chroma、原始HTML、真实工作空间数据、报告、日志和既有压缩包；只例外保留经过脱敏的 `data/demo/portscope_demo.db`，以及源代码、测试、配置模板、`.env.example`、README和批处理启动文件。每个压缩包还包含`release_manifest.json`，记录产品版本、构建时间及每个交付文件的大小和SHA-256，便于验收和升级追溯。

## 测试

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

测试覆盖原有事件、草稿、SSRF、生命周期、风险评分、质量和报告，以及页面密钥保存/清除、密钥不入库、27工具注册、Pydantic拒绝、未注册工具拒绝、确认门禁、对话隔离、`reasoning_content`回传、循环停止、本地回退、单页来源接入、报告三文件、交付包排除规则和全部Streamlit页面。pytest不访问官方站点或DeepSeek。

真实来源健康验收不在pytest中自动运行。用户在“高级管理 → 更新公开数据 → 克制地验收真实来源”明确勾选后，系统每个来源只读1个列表页、发现最多5篇、下载最多2篇、不翻页、每次请求间隔至少3秒，并保存“正常 / 部分可用 / 结构变化 / robots或条款待确认 / 连接失败 / 暂停使用”健康度。

## 仍需人工处理的情况

- robots 或网站条款不明确；
- 商业再利用/收费报告许可未明确；
- JavaScript 渲染、登录、验证码、付费墙或访问控制页面；
- PDF、图片、扫描件和 OCR；
- 列表或正文选择器变化；
- 发布日期/机构无法可靠提取；
- AI 低置信度、异常 JSON、重复候选和生命周期关联；
- 中高风险和所有准备进入商业报告的内容。

系统不采集个人信息，不保存 Cookie 或登录凭据，不把客户内部数据发送给第三方，不做大规模批量抓取、浏览器自动化、验证码识别、自动定时任务、云部署或无人审核入库。

## 主要代码

```text
app.py                     AI工作台三页默认导航与高级管理入口
ui_agent.py                对话、API配置、进度、确认卡片和文件下载
agent/                     工具注册、Pydantic模式、执行器、确认、对话和编排
ui_platform.py             首页、更新、问答、审核、报告、专业设置
platform_db.py             SQLite 表、事务、CSV 迁移/导出和来源入口
crawler/                   白名单适配器、robots、安全获取和采集管理
document_processor.py      原始归档、哈希去重、版本链和差异
document_chunker.py        中文文本分块
deepseek_service.py        Pydantic 严格抽取、RAG回答和安全日志
event_pipeline.py          本地/AI事件抽取、规则评分与批量确认
source_health.py           需用户确认的克制真实来源健康验收
rag/                       本地Embedding、Chroma、FTS5、混合检索和引用
platform_report.py         SQLite合规筛选与商业报告入口
commercial_report.py       DOCX、HTML和Excel生成
package_release.py         排除环境、密钥、真实数据和缓存的交付打包
web_extractor.py           原有单URL SSRF安全提取器
data_validator.py          完整数据质量校验
risk_engine.py             透明确定性评分
lifecycle.py               风险生命周期
```

## 四周试运营前的数据质量门禁

自动采集会在调用 DeepSeek、写入 FTS5/Chroma 和创建事件之前执行正文质量检查：

- 大量 `å`、`ä`、`ç`、`Ã`、`�` 等疑似错误解码字符会标记为“编码异常”；
- 网站名称/“首页”标题、空正文、附件名称列表、重复导航、页头页脚占比过高、低信息密度和跨页面模板高度相似会被拦截；
- 发布日期缺失的资料不能直接进入报告；
- 列表没有日期时，正文提取后会再次按起止日期检查；范围外页面只归档，不创建本期事件；
- 质量异常文档保留原始 HTML 和审计记录，但不进入 AI、索引或报告。

历史数据库中 `quality_status=未检查` 的文档会在启动时进行一次安全复核。被拦截的历史事件不会物理删除，但会取消报告资格并移除 FTS Chunk。

### 官方文本型 PDF

自动采集只处理已启用白名单域名直接提供、robots/访问规则允许、未加密且在体积上限内的文本型 PDF。每次重定向重新核对协议、域名、DNS 和 IP；下载同时校验 `Content-Type`、URL 扩展名和 `%PDF-` 文件头，并保存原始 PDF、下载时间、大小和 SHA-256。内容哈希不变时跳过，变化时创建新版本并保留旧文件。

PDF 提取使用 BSD-3-Clause 许可的 `pypdf`，版本和商业分发评估见 `THIRD_PARTY_LICENSES.md`。提取结果还须通过乱码、有效文本长度、重复页眉页脚、导航/模板噪声、发布日期和信息密度门禁。不合格文件只保留审计归档，不调用 DeepSeek、不分块、不索引、不创建新事件，也不进入报告。

本轮不支持 OCR、扫描图片 PDF、加密 PDF、复杂表格还原和图片公告识别。文件能够公开下载不等于允许全文转载或商业再销售。

黄金质量集位于 `qa/golden_documents/`，包含 32 个本地 HTML，均为虚构测试内容：

```powershell
.\.venv\Scripts\python.exe qa\build_golden_documents.py
.\.venv\Scripts\python.exe qa\evaluate_golden.py
```

结果写入 `qa/quality_report.json` 和 `qa/quality_report.md`。

### 隔离的真实资料验收

真实网站访问不放入 pytest。先执行 `backup_workspace.py`，再用 `qa/run_real_acceptance.py` 创建独立 `output/qa_real/<run-id>/`、SQLite 和 Chroma；正式 `data/portscope.db`、原始归档和历史报告不会被覆盖。

```powershell
.\.venv\Scripts\python.exe backup_workspace.py
.\.venv\Scripts\python.exe qa\run_real_acceptance.py --phase A
.\.venv\Scripts\python.exe qa\evaluate_real_acceptance.py --db "<QA目录>\portscope_qa.db" --workspace-id "<QA工作空间ID>" --labels qa\real_acceptance_labels.xlsx
.\.venv\Scripts\python.exe qa\run_real_acceptance.py --phase B --run-root "<QA目录>"
```

阶段 A 每来源最多 2 篇。只有由 `human_user` 或 `industry_reviewer` 独立完成、具有姓名/时间/方法/版本的人工标签完整，且标题≥95%、日期≥90%、机构≥95%、正文≥90%、DeepSeek≥90%、证据可追溯≥90%，才允许扩大；`ai_assistant`、`codex_agent`、`automatic_rule` 和来源不明的历史标注均不计入人工金标准。若第三方官方来源不稳定、许可不清晰或高价值资料不足，应按实际合格数量停止，不能用宣传稿或不安全来源补足 30 篇。

`qa/real_acceptance_report.md` 和 `.xlsx` 分开显示自动流水线指标、独立真人金标准、AI/Agent/规则标注与待核验项。未完成独立真人标注时必须显示“待人工核验”，不得声称真实准确率 100%。真实标签、QA 数据库、原始文件、向量库和报告均被 `.gitignore` 与交付打包脚本排除。

### 证据绑定与 QA 晋升

DeepSeek 或规则返回的每个证据片段都会记录 `document_id`、版本、`content_hash`、字符起止位置和验证状态。系统先做逐字查找，失败时只允许忽略空白和换行；语义近似或标点改写不能冒充逐字证据。文档更新后旧引用自动失效。引用未定位的事件不会进入执行摘要或客户交付版，内部研究版会明确提示回原文核验。

QA 数据不能整库覆盖正式数据库。完成独立真人逐项核验后，由管理员按事件执行：

日常使用优先在“人工审核”完成连续核验：可筛选未审核、接受、修改后接受、驳回和待二审记录，暂存内容不会计入真人审核；只有点击保存并完成原文/事实核对清单后才写入审核链。正式库为空而QA存在候选时，首页会显示“审核 → 证据修复 → 待晋升队列”三步向导，但不会自动执行任何一步。

```powershell
.\.venv\Scripts\python.exe promote_qa_data.py `
  --qa-db "<QA数据库>" --formal-db "data\portscope.db" `
  --source-workspace-id "<QA工作空间>" --target-workspace-id "<正式工作空间>" `
  --qa-run-id "<QA运行ID>" --event-id "<事件ID>" `
  --report-mode "内部研究版" --confirm "我已确认逐条核验"
```

只有正文质量合格、非高度疑似重复、当前版本引用全部可定位、来源与URL完整、事件未隔离/驳回，且核验来源为 `human_user`/`industry_reviewer` 的事件才能晋升。批量晋升是全有或全无事务，任一所选记录失败时整批回滚；晋升保留原 `document_id`、URL、哈希、QA运行ID、核验人与时间、事件审核链，并写入 `promotion_history`、`operation_runs` 和FTS索引，Chroma状态标记为待向量化。

正式库中的两项资格相互独立：

- `active_for_internal_use`：通过真人审核、逐字证据、正文质量、来源完整与去重门禁，可供内部检索和分析；
- `eligible_for_customer_report`：在内部资格之上，还必须通过客户摘要、必要短引用和客户报告规则。

两项资格都不代表允许第三方全文再分发或出售原始数据库。

“来源与系统设置 → 四周评估”只汇总 `crawl_source_runs` 中真实发生的逐来源采集记录，包括请求、成功、跳过、HTTP/TLS/超时、正文质量失败和耗时。没有实际运行时天数保持0；客户反馈只能由用户手工录入。

## 报告真实性规则

- 内部研究版逐项显示实际人工确认状态，不会把 `human_verified=0` 写成“经人工确认”；
- 客户交付版继续要求独立真人确认、报告资格、逐字证据和摘要/短引门禁；许可未明确会显示风险提示而非阻断整个内部工作台；
- DOCX 主体默认最多 5 条核心事项，活动、党建、荣誉和普通培训只保留在完整 Excel，不占用执行摘要和核心风险；
- 风险回答分为当前有效、已解除留档、影响环节、核对动作和证据不足；青岛港查询优先青岛辖区及黄海相关航线，不以威海、日照等城市信息作为主答案；
- 乱码、模板噪声、通用网站标题、日期缺失和低置信度未确认内容不进入执行摘要或核心事件；
- 0 条合格事件时默认阻止生成完整 DOCX/HTML/Excel；
- 如只需要版式，必须显式勾选“没有合格事件时仅生成空白模板”，文件会标记为“空白模板，不是正式报告”。

## 工作台备份

双击 `backup_workspace.bat` 可备份 SQLite 快照、原始 HTML、工作空间报告和项目配置。备份不会包含 `.venv` 或 `.env/API Key`。普通代码交付仍使用 `package_release.bat`，并排除环境、密钥、真实数据库、Chroma、原始 HTML、报告、日志、备份和历史压缩包。

交付前运行 `.\.venv\Scripts\python.exe security_audit.py`，它会扫描源码、报告、日志和历史 ZIP 中是否存在疑似密钥或 `.env`，但不会读取当前项目根目录 `.env`，也不会输出任何密钥内容。临时测试密钥轮换步骤见 `rotate_test_key_checklist.md`。若解压后的发行包同时包含 `release_manifest.json` 和 `.env`，应用启动时会显示安全警告。

## 商用边界

当前商业形态是项目所有者使用本地工作台提供公开信息研究、人工核验简报、风险提醒和定制分析，而不是出售软件安装包或第三方原始数据库。客户交付版必须由来源/证据、数据质量和独立真人审核门禁共同放行；内部研究版不能改名冒充客户交付版。

正式签约前还需要经营主体完成客户合同、隐私与AI告知、来源条款证据、支持等级、责任限制和适用法律的人工确认。工程侧清单见`COMMERCIAL_DEPLOYMENT_CHECKLIST.md`；第三方依赖、Embedding模型及DeepSeek外部服务记录见`THIRD_PARTY_LICENSES.md`。

当前来源说明见`config/source_license_evidence.json`。新数据模型把 `internal_collection_allowed`、`internal_analysis_allowed`、`customer_summary_allowed`、`short_quote_allowed`、`fulltext_redistribution_allowed` 和 `raw_data_resale_allowed` 分开。许可说明缺失不会关闭内部工作台，但会限制全文/原始数据导出并在客户简报中提示风险。

## 当前真实来源限制（2026-07-28 小规模验收）

- 山东海事局海上风险预警：专用适配器可从 XML/CDATA 列表识别标题、日期和链接，并安全跟进白名单内文本型 PDF。正式库已受控返工 2 篇历史附件名占位记录，2 篇均创建 PDF 新版本并通过正文质量门禁；旧 HTML 错误版本仍隔离留档。这是自动流水线验收，不是独立真人核验。
- 山东海事局政务动态：旧阶段 A/B 自动流程样本均完成，尚待独立真人逐条核验；活动宣传类稿件保留为低业务价值，但排除执行摘要、核心风险和高价值 RAG 查询。
- 山东省港口集团要闻：既有正式样本没有产生合格正文，包含空正文、通用标题和编码异常；当前标记为 `needs_adapter` 并停用自动批量采集，只保留人工 URL、HTML 或文件导入。
- 山东海事局通知公告、安全形势分析：当前主要附件为 DOC/PPTX 或信息不足的示意图，本轮不解析，已暂停。
- 青岛市交通运输局栏目：本机 TLS 证书链校验失败；系统不会关闭证书验证绕过。
- 青岛海洋预报入口：本机连接超时，保持停用。

本轮在隔离 QA 工作空间停在 15 篇当前文档，其中 10 篇通过正文质量门禁并完成 DeepSeek 抽取；没有为达到 30 篇而扩大到低价值、不安全或许可不清晰来源。当前版本适合受控内部试运营，不应表述为已稳定覆盖 3–5 个不同机构，也不具备无人值守客户交付条件。
