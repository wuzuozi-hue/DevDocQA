# DOC-22：Redis 数据结构

String 可保存简单值、计数或序列化对象；Hash 适合对象多个字段；Set 适合去重与成员判断；Sorted Set 用分数排序；List 适合简单队列。根据访问方式选结构，不要把所有需求都塞进一个巨大 JSON。

键名采用一致的冒号分段，例如 `devdocqa:limit:<keyhash>:<window>`。Key 的命名空间帮助管理，API Key 原文不应出现在键名或日志中。TTL 为缓存和临时计数设定存活时间。

Redis 不是本项目任务结果的唯一持久存储，任务与答案仍保存在 PostgreSQL。选择数据结构要同时说明更新方式、过期策略和故障时行为。
