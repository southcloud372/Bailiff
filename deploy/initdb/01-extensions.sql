-- 首次初始化时执行（仅当数据卷为空）。幂等。
CREATE EXTENSION IF NOT EXISTS vector;
-- 生成列/模糊匹配的辅助扩展（可选，按需开）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- HNSW 索引由应用侧 ensure_schema() 建（需要知道维度），这里只保证扩展可用。
