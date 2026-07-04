# M9 — 向量化 / RAG 扩展（可选延伸，同样延后）

> 前置依赖：[M7-production-hardening.md](./M7-production-hardening.md)（与 M8 同级，均延后，互不阻塞）
> 状态：延后
> 关联文档：[03-architecture-proposal.md](../03-architecture-proposal.md) §8 未来扩展：向量化 / 自建 RAG · [04-roadmap.md](../04-roadmap.md) §4 M9

## 范围（到时候再细化）

不新增数据库，复用同一 Postgres 实例的 `pgvector` 扩展；embedding 模型选型待独立调研。

## 备注

- 这是"可选延伸"，不是必须完成的里程碑；是否启动取决于届时是否有明确的检索/RAG 需求。
- 启动前先读 03 §8 的完整讨论，再决定是否需要为 `articles`/`documents` 表新增 `embedding` 列（03 §3 SQL 中 `articles.embedding VECTOR(1536)` 已预留但注明"未启用 pgvector 前此列不建"）。
