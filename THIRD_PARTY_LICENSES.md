# 第三方依赖与模型许可说明

本文件记录PortScope直接运行依赖、模型和外部AI服务。实际锁定版本见`requirements-lock.txt`，完整依赖树必须在每次正式发布时随发行清单复核。本表是工程记录，不替代法律意见。

## 直接运行依赖

| 组件 | 锁定版本 | 用途 | 许可证 |
|---|---:|---|---|
| Streamlit | 1.50.0 | 本地用户界面 | Apache-2.0 |
| pandas | 2.3.3 | 表格处理与导出 | BSD-3-Clause |
| requests | 2.32.5 | 受控HTTP请求 | Apache-2.0 |
| Beautiful Soup | 4.15.0 | HTML解析 | MIT |
| python-docx | 1.2.0 | DOCX生成 | MIT |
| Pydantic | 2.13.4 | 数据与工具参数校验 | MIT |
| python-dotenv | 1.2.1 | 本地环境变量配置 | BSD-3-Clause |
| pypdf | 6.14.2 | 未加密文本型PDF解析 | BSD-3-Clause |
| ChromaDB | 1.5.9 | 本地向量索引 | Apache-2.0 |
| sentence-transformers | 5.1.2 | 本地Embedding加载 | Apache-2.0 |

上述均为宽松开源许可证，但分发时仍需保留适用的版权、许可证和NOTICE信息。ChromaDB 1.5.9的Python元数据未提供标准`License-Expression`，工程核对使用其官方PyPI和仓库所列Apache-2.0：

- https://pypi.org/project/chromadb/
- https://github.com/chroma-core/chroma

Streamlit与sentence-transformers的官方仓库分别声明Apache-2.0：

- https://github.com/streamlit/streamlit
- https://github.com/huggingface/sentence-transformers

`pypdf`只用于文本提取；本项目不启用OCR、不处理加密PDF，也不使用图像/OCR额外依赖。

## 本地Embedding模型

- 模型：`BAAI/bge-small-zh-v1.5`
- 用途：在客户本机生成中文文本向量。
- 模型卡许可证：MIT。
- 模型卡声明已发布模型可免费用于商业用途。
- 官方模型卡：https://huggingface.co/BAAI/bge-small-zh-v1.5

正式离线交付模型权重时，应记录具体仓库修订哈希，并随交付保留模型卡和许可证；不得仅记录可变的`main`分支名称。

## DeepSeek API

DeepSeek是可选外部服务，不随发行包分发。开放平台条款允许把API能力集成到面向内部或外部最终用户的下游系统，同时要求开发者承担下游系统安全、用户告知、个人信息处理和合法使用责任。项目不得暗示与DeepSeek存在官方合作或认证关系。

- 开放平台条款：https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html
- 使用条款：https://cdn.deepseek.com/policies/en-US/deepseek-terms-of-use.html
- 隐私政策：https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html

## 发布复核

许可证信息核对日期：2026-07-23。每次正式商业发布必须：

1. 从干净虚拟环境安装`requirements-lock.txt`；
2. 导出实际安装版本和许可证元数据；
3. 复核所有传递依赖和模型权重；
4. 将必要的LICENSE/NOTICE随客户发行包提供；
5. 重新检查DeepSeek条款、来源条款和客户所在法域要求。
