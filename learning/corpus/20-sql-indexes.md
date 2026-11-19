# DOC-20：索引与查询验证

索引需要匹配过滤、连接和排序方式。对 `WHERE status='ready' ORDER BY created_at DESC, id DESC LIMIT 10`，可考虑 `(status, created_at DESC, id DESC)` 复合索引，或只包含 ready 文档的部分索引。

是否采用索引取决于表规模、分布和查询计划，不能只凭存在索引就断言更快。使用代表性数据，比较查询计划、实际执行时间和扫描行数；EXPLAIN ANALYZE 会真实执行查询，使用时理解其影响。

索引占用存储，插入、更新和删除需要维护，过多索引可能拖慢写入。先测量瓶颈，再增加最小必要索引，保存优化前后的同条件结果。
