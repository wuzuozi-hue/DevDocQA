# 评测与压测操作

## 先区分三种结果

- Mock 链路测试：检查接口、任务、数据库、限流、引用格式；不能判断语言模型能力。
- 真实模型冒烟：一份短文档跑通真实 Embedding 和生成；不能代表完整质量达标。
- 冻结测试集评测：固定模型、文档、分块和参数，记录全部问题，人工判断答案及来源支持程度。

`learning/eval/questions.jsonl` 是原创教学夹具，不是公开标准基准。只把 `learning/corpus/` 文档上传，题目和答案不得作为检索资料。

## 开发集

完整服务启动后，运行：

```bash
python scripts/evaluate.py --split dev --ingest
```

真实 Ollama 模式下可同时比较无检索直接回答：

```bash
python scripts/evaluate.py --split dev --ingest --baseline
```

脚本逐题等待任务，遇到 429 会等待限流窗口。使用 `--ingest` 时，逐题把文档范围限定为本次语料。每题写一次检查点，结果默认保存在 `reports/generated/evaluation.json`。`retrieval` 用于计算前 5 个候选的文档命中，`evidence_trace` 用于计算入模文档命中，`citations` 用于人工检查最终引用是否支持回答。缺少追踪时，入模文档命中保持未知。执行失败的题单独计数。

调参仅使用 dev20。模型、分块与检索参数冻结后，再明确执行 `--split test --baseline`，并用独立 `--output` 路径保存，不能覆盖开发集结果。

## 人工评分

逐条检查并填写 JSON 记录中的 `answer_correct`、`citation_supported`、`baseline_correct` 为 true/false，解释记在 `review_notes`。没有评分的值保持 null。

```bash
python scripts/score_report.py reports/generated/evaluation.json
```

直接题 Hit@5 要包含其参考文档；多文档题要覆盖全部指定文档；无答案题不计入 Hit@5 分母。答案的“资料不足”判断不能仅靠文字短语，需结合问题和参考资料人工核对。

测试集目标：Hit@5>=80%，人工回答正确率>=75%，无答案题依据不足识别>=80%。只在相应真实结果通过时写入简历；未知值不能算成功。

## 模拟模型负载

使用 `MODEL_MODE=mock`，另设本地压测实例的 `MOCK_DELAY_SECONDS=0.3`、较高限流阈值；不要对重要业务服务关闭保护。脚本使用不同问题以避免缓存干扰：

```bash
python scripts/benchmark.py --document-ids 已就绪文档ID
```

记录1/10/50并发客户端的提交延迟、HTTP拒绝状态、成功/失败任务及完成耗时。429和503是容量控制结果，不计为成功吞吐。CPU/内存由操作系统监控另行记录，不能凭请求延迟推算。

同一模拟环境下可运行 `python scripts/benchmark.py --mixed --output reports/generated/mixed.json`。脚本每组先提交 6 个文档，再同时提交 2 个问答，记录问答完成时间、问答提交时的入库状态和入库吞吐。对照共享调度和隔离调度时，固定两个版本的总执行名额、总等待容量和 Worker 延迟。若提交问答时入库已处理完，该组只代表无入库竞争的负载。

本次原生测量文件是 `reports/native-benchmark.json`，报告标明其并发/队列/限流覆盖值、Redis测试替身和模型固定延迟。后端原生构建使用未优化标志，不能据此宣称生产性能或优化收益。
