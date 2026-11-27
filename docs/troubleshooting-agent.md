# 开发文档排障 Agent

遇到跨文档的问题，一次 Top 5 检索可能只找到部分原因。例如同一个 HTTP 503 可能来自队列已满或 Redis 不可用。调用方指定 `strategy: "agent"` 后，模型根据当前证据选择下一步。普通问答仍是默认方式和评测基线。

## 调用流程

1. C++ 网关校验输入，解析获准检索的文档集合，把策略写入持久化任务。两种策略共用原有的队列、并发限制和终态写库。
2. Python Worker 把问题和本轮收集的证据摘要交给模型。模型返回结构化动作，Worker 验证参数再调用工具。
3. `search_docs` 对模型给出的查询做向量检索，只查本任务的文档范围。`read_section` 按已发现的证据编号读同一文档的相邻分块。文件内容和文件名作为不可信数据传给模型，Worker 只提供这两个只读工具。
4. 模型可选定最多五个片段并生成带源文的答案，也可声明证据不足。最终引用编号重新从 S1 开始，与 `agent_trace.selected_evidence_ids` 中的顺序对应。轨迹包含工具、执行结果、证据编号和耗时。

问答最多进行四次决策，Worker 的 Agent 预算为 85 秒，同时受 C++ 任务剩余时间约束。重复查询和重复读取会被跳过。用完步骤时返回证据不足，超时任务按失败处理。Redis 按策略隔离答案，Agent 证据不足的结果可以重试。

## 做对照评测

把服务切到真实模型后，在独立环境跑同一批问题。先确保环境里没有前一次试验的答案缓存。

```bash
python scripts/evaluate.py --strategy both --split dev --ingest --output reports/generated/agent-comparison.json
```

可追加 `--case Q041 --case Q053` 只检查指定题目。报告记录两种策略的答案、引用、提示实际包含的文档、耗时、缓存命中和 Agent 轨迹。人工按参考答案核对结论是否正确、引用能否支撑对应结论。统计模型耗时和效果时单独列出缓存命中的请求。Mock 模式可验证服务和工具接线，真实效果以接入模型的评测为准。

### 2026-11-27 本地验证

使用真实 C++ API、隔离 PostgreSQL + pgvector、本地 Redis 和 Ollama。用 5 篇教学文档跑了三组未命中缓存的配对请求：

| 用例 | 普通问答 | 排障 Agent |
| --- | --- | --- |
| Q041 事件循环与两类延迟 | 作答并引用两篇相关文档，6.1 秒 | 作答并引用同两篇文档，8.1 秒；对总耗时起点的说法前后不一致 |
| Q043 更换向量与生成模型 | 当轮任务返回 `worker_failed`，另一轮曾正常作答 | 引用向量和缓存两篇文档，12.1 秒 |
| Q053 题库实际正确率 | 给出证据不足，2.0 秒 | 给出证据不足，4.1 秒 |

另用一篇包含多个分块的 503 排障手册检查工具链。真实模型先检索，再读相邻片段；重复查询被拦下，最后从引用中区分 `queue_full` 与 `redis_unavailable`。单独复提了 Q041 的两种策略，Redis 分别命中各自的答案。

页面演示可上传[示例手册](../learning/examples/gateway-incident-handbook.md)，只选择这篇文档，切换到 Agent 后提交：

> Gateway incident handbook: use search to find the 503 incident section and use read_section to inspect its adjacent chunks before deciding. Which two exact error codes distinguish queue saturation from Redis unavailability?

耗时来自单次本地 API 提交与轮询，包含轮询间隔。样本范围限于上述用例，尚未用完整测试集确认准确率收益。

| 模块 | 源码 |
| --- | --- |
| 决策循环、工具参数校验、证据编号 | [worker/app/agent.py](../worker/app/agent.py) |
| 结构化模型调用与提示预算 | [worker/app/providers.py](../worker/app/providers.py) |
| 范围内检索与相邻分块读取 | [worker/app/store.py](../worker/app/store.py) |
| 策略入队、超时及 Redis 策略隔离 | [backend/src/server.cpp](../backend/src/server.cpp) |
| 配对评测脚本 | [scripts/evaluate.py](../scripts/evaluate.py) |
