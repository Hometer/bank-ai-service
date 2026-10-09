<div align="center">

![银行智能客服](docs/assets/readme-cover.svg)

# 银行智能客服

**让每一次咨询，都有依据。**

[快速开始](#快速开始) · [配置说明](docs/配置说明.md) · [文档导航](docs/README.md) · [项目状态](docs/项目状态.md)

</div>

## 项目介绍

围绕交通银行（香港）公开资料的个人与企业咨询演示。优先查本地知识库，回答展示来源；可使用 DeepSeek 整理资料与受控官网内容，另提供普通话语音入口。

## 界面预览

![银行智能客服实际界面](docs/assets/readme-preview.png)

实际本地咨询页，来自 2026-10-04 资料问答验收。文本曾使用真实 DeepSeek；人物为静态展示，不能视为银行服务或数字人口型效果。

## 核心功能

- 资料咨询：本地知识库、个人与企业服务目录、来源查看与无依据提示。
- 多轮问答：当前问题优先、话题绑定、追问与历史会话恢复。
- 语音入口：浏览器识别、本机 Whisper 兜底、确认转写、可打断播报及 macOS 语音回退。
- 人工参考：生成明确标注的辅助草稿；官网获取限定范围，费用与请求记录单独保存。

## 快速开始

准备 Python 3.12；基础文字知识库无需 Node.js。语音依赖浏览器能力；本机识别另需 Whisper CLI / 模型与 FFmpeg，macOS 播报可使用系统语音。 下列命令适用于 macOS / Linux。先安装依赖：

```bash
git clone https://github.com/Hometer/bank-ai-service.git
cd bank-ai-service
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cp .env.example .env
```

启动服务：

```bash
# 从仓库根目录启动知识库演示，无模型费用
MODEL_MODE=demo PUBLIC_KNOWLEDGE_ENABLED=1 .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 18768 --no-access-log
```

打开 <http://127.0.0.1:18768>；API 文档在 <http://127.0.0.1:18768/docs>。按 `Control+C` 停止对应服务。

## 配置说明

首先运行知识库演示，使用非敏感公开问题。`.env` 配置真实服务时需要自己的 `MODEL_API_KEY`、`MODEL_BASE_URL` 和 `MODEL_NAME`。V1.5 语音与联网答复还有独立费用台账、服务开关与本机识别依赖，按配置说明启用；仓库不包含旧授权额度或私人台账。

详细参数、数据位置和工程检查见 [配置说明](docs/配置说明.md)。仓库不包含本机密钥、业务数据、依赖目录或运行缓存。

## 当前范围

本地公开知识演示，未获银行审核，未连接账户、真实坐席或生产认证。顾问图片为展示素材，没有口型视频或全双工能力；物理麦克风、真实听感、真机与 V1.5 产品验收仍待完成。知识内容须按来源日期再次核对。

以 [项目状态](docs/项目状态.md)、当前 PRD 与对应验收记录为准。历史测试与截图标明其范围，本次上传不新增产品验收结论。

## 文档与代码

`backend/app/` 为 FastAPI 与静态网页，公开知识与来源存于 `docs/知识库/`。SQLite 会话、模型文件和预算台账在被忽略的 `data/`。

[文档导航](docs/README.md) · [当前需求](docs/PRD/%E9%93%B6%E8%A1%8C%E6%99%BA%E8%83%BD%E5%AE%A2%E6%9C%8D_PRD_V1.5_%E8%AF%AD%E9%9F%B3%E4%B8%8E%E8%81%94%E7%BD%91%E7%AD%94%E5%A4%8D%E5%A2%9E%E9%87%8F.md) · [配置与检查](docs/配置说明.md)

<details>
<summary>展开开发手册、需求与验收记录</summary>

- [需求与 PRD](docs/PRD/)
- [开发手册与操作说明](docs/手册/)
- [技术适配与阶段开发文档](docs/阶段文档/)
- [验收记录与实际界面证据](docs/evidence/)
- [历史首页与分期开发记录](docs/历史/开发记录.md)
- [本次仓库整理记录](docs/仓库整理记录.md)

</details>

## 许可与来源

保留原项目 [LICENSE](LICENSE) 与作者署名。项目由 AI 产品开发工具包起步；历史规则与必要文档保留，新增整理以实际实现为准。
