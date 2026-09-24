# 飞书样机需求跟催机器人

基于 FastAPI 的飞书样机需求跟催服务。当前已完成阶段 1：读取普通飞书电子表格、识别指定硬件阶段的未填写项、通过申请人邮箱映射 `open_id`，并生成 24 小时有效的只读预览快照。消息发送尚未实现。

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

访问 `http://127.0.0.1:8000/`，健康检查为 `GET /health/live` 和 `GET /health/ready`。缺少飞书凭据或 Redis 不可用时，存活检查仍成功，就绪检查返回 `503`。

阶段 1 接口：

- `POST /api/v1/previews`：创建预览；
- `GET /api/v1/previews/{previewId}`：读取预览。

所有 API 均使用 `success/code/message/data` 统一响应外壳。阶段 2 的发送与失败重试路由当前不开放。

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
  services/         模板解析和预览聚合业务逻辑
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
