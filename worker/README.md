# DevDocQA internal worker

Python 3.11+。在本目录安装 `python -m pip install -r requirements-dev.txt`，然后运行
`python -m app`。测试：`python -m pytest`。
数据库表由项目根目录的迁移建立；worker 不自行修改数据库结构。
原生 Windows 启动器会设置 psycopg 所需的 SelectorEventLoop；Linux/Docker 也可以直接运行
`uvicorn app.main:app --host 0.0.0.0 --port 8001`。需要真实数据库的测试设置 `PG_DSN_TEST`
后执行 `python -m pytest -m integration`，未设置时明确跳过。

## 环境变量

| 变量 | 默认值 / 用途 |
|---|---|
| `PG_DSN` | `postgresql://devdocqa:devdocqa@localhost:5432/devdocqa` |
| `INTERNAL_KEY` | 必须与 C++ 服务相同；未配置则拒绝内部请求 |
| `DATA_DIR` | `data/documents`，上传文件允许的根目录 |
| `MODEL_MODE` | `mock`；真实本地模型使用 `ollama` |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434`；原生运行时改为 `http://127.0.0.1:11434` |
| `CHAT_MODEL` / `EMBED_MODEL` | `qwen3:4b` / `bge-m3` |
| `MODEL_TIMEOUT_SECONDS` | 90，每次模型 HTTP 请求超时；整个任务的 120 秒期限由 C++ 控制 |
| `MOCK_DELAY_SECONDS` | 0；模拟回答的固定等待时间，用于压测，与真实模型性能无关 |

`mock` 使用归一化的 1024 维词元哈希和**原文摘录**，没有调用语言模型，不能评价真实
语义检索或生成效果。小测试集中的相关词元得分高于 0.10、不相关词元低于 0.10；该阈值
只是可测的模拟规则，不是生产语义模型阈值。

## 内部契约

`GET /health` 只表示进程存活。其余接口均要求 `X-Internal-Key`，不暴露到公网。

- `POST /internal/ingest`：`job_id, document_id, path, name, sha256`；成功返回
  `{"document_id":"…","chunks_count":N,"embedding_signature":"…"}`。仅允许 DATA_DIR 中的 `.md`/`.txt` 文件；检查
  UTF-8、2 MB 上限和 SHA-256。先生成全部向量，然后事务替换片段。事务先锁定 running
  job，再锁定 processing document；过期任务返回 409，不会晚到写入失败任务的片段。
- `POST /internal/answer`：`job_id, question, document_ids`；空 ID 列表表示所有 ready 文档，
  非空列表限制检索范围。精确 cosine 排序返回前 5 个片段，不依赖近似索引。问题最长 4000 字。
  结果含 `answer, citations, retrieval, insufficient_evidence, model_mode, timings`。
  `retrieval` 保留检索前 5 个候选，供 Hit@5 评估；`citations` 只含模型实际引用的候选。
- 引用字段：`source_id, document_id, document_name, chunk_index, excerpt, score`；片段序号从 0 开始。
  `source_id` 是本次 retrieval 中的 1—5 编号，即使 citations 只含原来的第 3 个候选也保留 3，
  对应模型文本 `[S3]`，不能按 citations 返回位置重新编号。
  模型引用用 `[S1]` 等标记，worker 验证编号均属于此次提供的证据；`excerpt` 为完整片段，
  最长 800 字。编号验证无法证明每一句语义正确，仍需冻结测试集人工评估。
- 错误响应：`{"error":{"code":"stable_code","message":"sanitized explanation"}}`。
  校验 422、鉴权 401、存储 503、模型错误 502、模型超时 504。

worker 只写 `chunks`；C++ 负责文档和任务状态、集合版本及缓存失效。
结构化引用编号有效但正文遗漏标记时，服务追加确定的“参考片段”标记；这仅规范展示，
不能证明语义正确。仅省略号的占位回答会被拒绝。
真实 prompt 使用 7168 UTF-8 字节保守输入预算（8192 context 减 768 输出和 256 模板余量），
超预算时删除排名最低的完整片段，不裁剪片段内部；问题本身或第一条证据无法容纳时返回
422 `context_too_large`。字节上限是当前字节分词模型的保守策略，不宣称字节等于 token。
片段记录向量空间签名 `mock:featurehash-v1` 或 `ollama:<EMBED_MODEL>`，C++ 成功入库时把
同一个签名保存到文档元数据。选中的 ready 文档有签名不匹配时返回 409
`embedding_mismatch`，需要在当前模式重新上传建库；相同 1024 维不代表向量空间相同。
后端断开超时的请求后，worker 在 250 ms 轮询中取消本地 HTTP/SQL 等待；这不能保证
Ollama 立即停止底层计算，因此仍保留入库时的任务状态检查。
真实模型必须返回 1024 维向量；文档证据作为 JSON 数据交给 Ollama，明确要求不执行其指令。
Ollama 本地模型无需 OpenAI API 密钥。未配置或下载真实模型时，请保持 `mock`。
