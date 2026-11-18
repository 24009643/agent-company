# Agent Company · 企业知识助手与 HR 工作台

Agent Company 是一个以检索增强生成（RAG）为底座的企业 AI 应用原型，面向企业知识问答和招聘辅助两个场景，支持在本地运行。

**企业知识助手**支持导入制度、流程和业务文档。员工可以用自然语言提问，系统在其权限范围内检索相关资料，并在回答中标注出处。资料不足时，系统会说明无法确认。

**HR 工作台**支持简历解析、岗位要求拆解、候选人对比和人工复核。系统把简历中的相关经历和待核实项整理出来，方便 HR 回看原文。候选人资料独立存储，是否推进由 HR 决定。

当前版本内置示例文档和演示账号，可在本地体验知识问答和招聘复核的主要流程。

## 在本地运行

安装 Python 3.11 和 [Ollama](https://ollama.com/)。启动 Ollama 后，下载对话模型和检索模型：

```bash
ollama pull qwen2.5:7b
ollama pull deepseek-r1:7b
ollama pull bge-m3
```

从项目根目录运行以下命令。Windows PowerShell 请把 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`。

```bash
cd backend
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 [本地首页](http://localhost:8000)。首次启动时，系统会创建演示账号，并使用 Ollama 为示例文档建立索引。

| 账号 | 演示密码 | 可以体验 |
| --- | --- | --- |
| `staff` | `staff123` | 查询全员可见的制度与流程 |
| `hr` | `hr123` | 查看人才库和岗位，复核候选人材料 |
| `hr_editor` | `hreditor123` | 配置并确认岗位评估规则 |
| `admin` | `admin123` | 管理知识文档和账号 |

## 演示边界

系统默认使用本机 Ollama，运行数据保存在 `backend/data/`。建议使用虚构简历体验招聘流程。演示账号仅供本地使用，请保持服务监听在 `127.0.0.1`。批量邮件的 SMTP 默认未配置。
