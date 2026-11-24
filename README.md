# DevDocQA 开发文档问答后端

面向开发文档的检索增强问答服务。C++ / Drogon 提供上传、任务调度和查询接口，Python / FastAPI 负责文档分块、向量检索和模型调用。任务存入 PostgreSQL，Redis 用于限流和缓存。

## 已提供的工程能力

- C++17 / Drogon 异步 HTTP、PostgreSQL、Redis；文档上传、持久任务、轮询结果。
- 有界调度、固定窗口限流、精确缓存、总时限、重启恢复和依赖错误反馈。
- Python / FastAPI 文档校验、分块、pgvector 检索、来源引用和模型适配。
- Mock 模式，以及本地 Ollama 的 `qwen3:4b` + `bge-m3` 接入。
- 简单演示页面、Compose、CI、测试、压测和评测脚本。
- 30 份原创教学文档、60 道 dev/test 分离题目，可用于检索与问答评测。

已有验证和适用范围见 [验证记录](reports/verification.md)，整体设计见 [架构与约束](docs/architecture.md)。

## 本地启动

需要 Docker Compose 和 Python 3.11+。命令生成本地配置并构建服务。

在本目录运行：

```bash
python scripts/init_env.py
docker compose up --build -d --wait
python -m pip install httpx
python scripts/smoke_test.py
```

然后打开 `http://localhost:8080`，在页面输入本地 `.env` 中的 `API_KEY`。不要把 `.env` 提交到仓库；页面不保存 Key。先上传 `learning/corpus/01-cpp-raii.md`（或任意教学文档），等待就绪，再提问。不要上传题库和参考答案，以免评测泄漏。

默认是 **Mock 模式**：使用确定性特征向量和原文摘录，方便排查服务链路；它不是语言模型，也不能代表 RAG 真实效果。

停止服务保留数据：

```bash
docker compose down
```

数据库和原文使用数据卷。初始化 SQL 只在首次创建 PostgreSQL 卷时执行；升级已有数据库时通过 `psql` 执行 `db/002_embedding_signature.sql`，不能靠重建容器代替迁移。

## 切换真实本地模型

在 Windows 主机安装并启动 Ollama，运行：

```bash
ollama pull qwen3:4b
ollama pull bge-m3
python scripts/set_model_mode.py ollama
docker compose up -d --force-recreate worker backend --wait
```

默认容器通过 `host.docker.internal:11434` 访问主机 Ollama。若主机 Ollama 仅监听回环地址，配置其监听地址并通过防火墙将访问限制在本机/容器网络；数据库、Redis 和 Worker 不需要公开端口。先用一份短文档验证模型连接，再扩大数据集。

**切换模型后重新上传已有文档**，系统会对相同内容重新建立索引。Mock 向量与真实向量不能混用；不兼容时返回 `409 embedding_mismatch`。切换生成模型也会改变缓存签名。

模型文件大小、运行内存和速度以本机实测为准。8GB 显存并不保证所有上下文长度都能全量 GPU 推理；必要时减少模型上下文或分批处理。

## 构建与测试

仅测试不依赖 Drogon 的 C++ 核心：

```bash
cmake -S backend -B build-core -DDOCQA_CORE_ONLY=ON
cmake --build build-core
ctest --test-dir build-core --output-on-failure
```

完整原生构建需要先安装 Drogon、OpenSSL、PostgreSQL 客户端、hiredis 及其依赖，然后：

```bash
cmake -S backend -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
python -m pip install -r worker/requirements-dev.txt
python -m pytest worker/tests -q
python scripts/validate_learning.py
```

设置 `PG_DSN_TEST` 可运行真实 PostgreSQL/pgvector 的 Worker 集成测试。必须使用专门的测试数据库；不要指向重要业务库。Windows 原生 Worker 使用 `python -m app`（从 `worker/` 目录），它会设置 psycopg 所需的 Selector 事件循环。

`scripts/verify_native.py` 可验证真实 C++ 服务和 PostgreSQL 链路，Redis 使用 TCP 测试替身并明确记录；它不能取代真实 Redis 的 Compose 验收。

## API 与配置

公开存活检查为 `GET /health`，依赖就绪检查为 `GET /ready`；全部 `/v1/*` 接口需要 `X-API-Key`。

| 接口 | 输入 | 返回 |
|---|---|---|
| `POST /v1/documents` | multipart `file`，UTF-8 `.md/.txt`，最多 2MiB | `202`，文档 ID、入库任务 ID、重复标记 |
| `GET /v1/documents` | 无 | 文档列表及状态 |
| `POST /v1/questions` | JSON `question`、可选 `document_ids` | `202`，任务 ID、缓存命中标记 |
| `GET /v1/jobs/{id}` | 任务 ID | 状态、结果、错误和时间 |

问答结果包含答案、实际使用的引用、检索候选、模型模式和分阶段耗时。问题最多 4000 UTF-8 字节，文档 ID 最多 200 个；空文档集合默认使用所有已就绪文档。状态为 `queued → running → succeeded/failed`。

主要配置在 `.env.example`。问答默认并发 1、等待容量 16，文档入库默认并发 1、等待容量 4，分别通过 `AI_CONCURRENCY` / `QUEUE_CAPACITY` 和 `INGEST_CONCURRENCY` / `INGEST_QUEUE_CAPACITY` 调整。`GET /health` 的 `scheduler.question` 和 `scheduler.ingest` 分别显示运行、排队及已预留名额。总时限 120 秒，每 Key 每分钟 10 个问题，缓存 600 秒。对应队列满返回 `503`，限流返回 `429`，输入问题返回 `400/413/415`，资料未就绪或向量不兼容返回 `409`。Redis 故障时新问答失败关闭，已运行任务继续完成。

真实模型问答的任务结果包含 `evidence_trace`，记录实际送入模型和被预算裁掉的来源编号，以及模型原始声明的引用编号。[优化清单](docs/optimization-log.md)

## 评测与项目范围

使用 [评测说明](docs/evaluation.md) 在自带样例上区分检索命中、答案质量与引用准确性。项目面向单用户、单实例、单轮问答。
