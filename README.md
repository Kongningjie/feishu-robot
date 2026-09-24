# 飞书样机需求跟催机器人

基于 FastAPI 的飞书样机需求跟催服务。阶段 1～3 的本地与 Mock 验收已完成：读取普通飞书电子表格并生成 24 小时只读预览快照；人工确认后按快照异步发送消息卡片；提供 Redis 幂等、逐人结果、自动重试、人工仅重试失败项、任务恢复、TPM 操作页面、安全控制、可观测性和双实例容器部署。真实飞书与公司基础设施联调尚待执行。

## 技术栈

- Python 3.11+
- FastAPI、Pydantic Settings、httpx
- Redis
- Jinja2、原生 JavaScript、CSS
- pytest、pytest-asyncio、respx
- Docker

## 本地启动

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
docker compose up -d redis
uvicorn app.main:app --reload
```

访问 `http://127.0.0.1:8000/`。默认 Compose 启动两个应用实例、共享 Redis 和本地验收网关；应用与 Redis 不直接映射宿主机端口。健康检查为 `GET /health/live` 和 `GET /health/ready`，指标为 `GET /internal/metrics`。缺少飞书凭据或 Redis 不可用时，存活检查仍成功，就绪检查返回 `503`。

当前接口：

- `POST /api/v1/previews`：创建预览；
- `GET /api/v1/previews/{previewId}`：读取预览和发送进度；
- `POST /api/v1/previews/{previewId}/send`：确认发送，返回 `202 Accepted`；
- `POST /api/v1/previews/{previewId}/retry-failures`：人工仅重试失败项，返回 `202 Accepted`。

所有 API 均严格使用 `success/code/message/data` 统一响应外壳。发送和重试接口不接收请求体，接收人完全由服务端快照和失败结果决定。

部署、回滚、监控及排障说明见 [docs/部署与运维.md](docs/部署与运维.md)。真实联调前必须轮换曾暴露的 Secret，并使用脱敏测试表与内部测试账号。

运行检查：

```powershell
.venv\Scripts\python.exe -m ruff check app tests
.venv\Scripts\python.exe -m pytest -q
```

## 目录

```text
app/
  api/routes/       HTTP 接口
  clients/          飞书和 Redis 访问封装
  core/             配置、日志
  models/           内部领域模型
  repositories/     Redis 预览快照存储
  services/         模板解析、预览聚合、卡片构建和发送编排
  static/           原生前端资源
  templates/        Jinja2 页面
docs/implementation-plan/
tests/
```

## 安全要求

- 不要提交 `.env`、App Secret 或访问令牌。
- 生产密钥由容器 Secret 或公司密钥管理系统注入。
- 曾经通过截图或终端暴露的 Secret 必须先轮换再使用。
- 日志、API 响应和工作日志不得包含完整邮箱、访问令牌、`open_id` 或表格标识。
