# DeepSeek 临时测试密钥轮换清单

本清单由项目所有者在每次真实 API 验收后人工执行。PortScope 不会读取、回显或自动上传密钥。

- [ ] 在 DeepSeek 控制台撤销本轮临时测试 Key。
- [ ] 创建权限和额度最小化的新 Key（仅在下一轮真实验收确有需要时）。
- [ ] 通过 AI 工作台的密码输入框保存到项目根目录 `.env`。
- [ ] 确认 `.env` 未进入 Git、备份、历史 ZIP、日志、报告或截图。
- [ ] 运行 `python security_audit.py`，仅处理其返回的文件路径，不复制其中内容。
- [ ] 运行 `python package_release.py` 并检查 `release_manifest.json` 中不存在 `.env`、数据库、QA 标签和报告。
- [ ] 如任何历史 ZIP 曾包含 `.env`，删除该交付副本并再次轮换 Key。
- [ ] 在客户环境使用客户自行持有的 Key，不共用开发或测试 Key。
