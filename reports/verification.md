# 验证记录 · 2026-10-13

这是执行记录，不能替代学生的能力诊断、整套模型质量评测或三个月学习成果。

## 实际通过

- C++17 完整服务在原生 Windows 成功构建；核心容量、UTF-8、配置与缓存材料测试通过。
- Worker 单元和实际 PostgreSQL/pgvector 集成测试通过；详见最终测试数量记录。
- 真实 C++ 公共 API 经过上传、鉴权、输入校验、重复上传、来源范围、精确缓存、集合版本失效、并发拒绝、Redis故障、限流、总时限和重启数据保留测试。
- Mock模式1/10/50客户端测量记录于 `native-benchmark.json`。Redis为 fakeredis TCP+Lua 测试替身，等待容量覆盖为4、生成延迟0.3秒，不是生产性能测试。
- Ollama v0.40.3 官方 Windows 包校验成功，`qwen3:4b` 与 `bge-m3` 下载并验证完成。模型和程序在工作目录内，没有配置付费API。
- 真实模型适配及 C++→Worker→PostgreSQL→Ollama 单文档问答成功；1024维向量、真实答案及引用已验证。见 `ollama-adapter.json` 和 `ollama-smoke.json`。
- 教学夹具校验：30篇原创文档、60题、dev20/test40、ID唯一、参考文件均存在。

## 测试条件

数据库测试使用原生 PostgreSQL18.6 与 pgvector0.8.7；Compose目标为 PostgreSQL16。这里的运行并不证明容器目标已通过。

API链路里的 Redis 为 TCP 测试替身，不是真实 Redis7.4。Compose以及CI已配置真实Redis集成验收，尚未在本机执行Docker流程。

原生测试曾遇到GCC工具链内部编译错误与运行库版本兼容问题；中间的GCC未优化构建用于已记录的模拟负载，不能据此声称优化收益。最终构建情况在下方追加。

## 最终交付复验（2026-10-13）

- 使用统一 Clang 工具链构建 Drogon/Trantor 和最终后端，Release 构建成功，C++核心测试1/1通过。
- 最终 Worker：48项通过，1项因Windows符号链接权限跳过；实际PostgreSQL/pgvector集成测试包含在通过项内。
- 通过 `run_native_demo.py --mode ollama` 启动最终后端、本地模型及Worker；`/ready`和演示页面均返回200。
- 最终公开API真实模型冒烟通过：存活、鉴权、输入校验、上传入库、问答、来源引用、未知任务。
- 本机演示地址为 `http://127.0.0.1:18880`；Redis仍明确为TCP测试替身，不能替代真实Redis部署验收。

## 尚待完成

- 本机 WSL2/Docker 完整环境及真实Redis容器验收；不能把提供的CI配置当作已运行的CI。
- AddressSanitizer/UndefinedBehaviorSanitizer检查：CI已配置核心测试，原生执行尚未证明。
- 固定test40的完整真实模型评测及人工答案/引用评分；一次RAII冒烟不能证明75%正确率。
- 对照优化实验、长期稳定性与CPU/内存测量。
- 学生独立诊断、15个岗位采样、简历事实填写与投递。模板没有自动伪造这些结果。

未创建远程仓库或向招聘方发送消息。本地Git仓库已初始化，`.env`、运行数据和缓存已忽略。
