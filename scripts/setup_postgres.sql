-- ============================================
-- PostgreSQL 数据库初始化脚本（运行时兼容版）
-- 高新医疗科技有限公司 SQLite -> PostgreSQL
-- ============================================

-- 连接目标数据库（需先 CREATE DATABASE）
\c udid_db;

-- ============================================
-- 1. 扩展与全文检索配置
-- ============================================
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 为避免环境缺失 zhparser 直接失败，默认使用 simple 配置。
DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese CASCADE;
CREATE TEXT SEARCH CONFIGURATION chinese (COPY = pg_catalog.simple);

-- ============================================
-- 2. products 表（主数据表）
-- ============================================
DROP TABLE IF EXISTS products CASCADE;
CREATE TABLE products (
    di_code VARCHAR(100) PRIMARY KEY,
    product_name TEXT,
    commercial_name TEXT,
    model TEXT,
    manufacturer TEXT,
    description TEXT,
    -- 运行时会写入空字符串，保持 TEXT 兼容旧逻辑
    publish_date TEXT,
    source VARCHAR(50) DEFAULT 'RSS',
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    category_code VARCHAR(100),
    social_code VARCHAR(100),
    cert_no TEXT,
    status VARCHAR(50),
    product_type VARCHAR(100),
    phone VARCHAR(100),
    email VARCHAR(200),
    scope TEXT,
    safety_info TEXT,
    search_vector tsvector
);

CREATE INDEX idx_products_search ON products USING GIN (search_vector);
CREATE INDEX idx_products_name ON products USING GIN (product_name gin_trgm_ops);
CREATE INDEX idx_products_manufacturer ON products USING GIN (manufacturer gin_trgm_ops);
CREATE INDEX idx_products_model_trgm ON products USING GIN (model gin_trgm_ops);
CREATE INDEX idx_products_commercial_name_trgm ON products USING GIN (commercial_name gin_trgm_ops);
CREATE INDEX idx_products_cert_no_trgm ON products USING GIN (cert_no gin_trgm_ops);
CREATE INDEX idx_products_category ON products(category_code);
CREATE INDEX idx_products_publish_date ON products(publish_date);
CREATE INDEX idx_products_last_updated ON products(last_updated);
CREATE INDEX idx_products_cert_no ON products(cert_no);

CREATE OR REPLACE FUNCTION products_search_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('chinese', COALESCE(NEW.product_name, '')), 'A') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.manufacturer, '')), 'B') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.model, '')), 'C') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.description, '')), 'D') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.commercial_name, '')), 'D') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.scope, '')), 'D') ||
        setweight(to_tsvector('chinese', COALESCE(NEW.cert_no, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tsvectorupdate BEFORE INSERT OR UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION products_search_update();

-- ============================================
-- 3. embeddings 表（向量存储）
-- ============================================
DROP TABLE IF EXISTS embeddings CASCADE;
CREATE TABLE embeddings (
    di_code VARCHAR(100) PRIMARY KEY REFERENCES products(di_code) ON DELETE CASCADE,
    embedding BYTEA,
    text_hash VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_embeddings_hash ON embeddings(text_hash);

-- ============================================
-- 4. 用户与鉴权
-- ============================================
DROP TABLE IF EXISTS users CASCADE;
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) DEFAULT 'admin',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login TIMESTAMP WITH TIME ZONE
);
CREATE INDEX idx_users_username ON users(username);

DROP TABLE IF EXISTS auth_audit CASCADE;
CREATE TABLE auth_audit (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    action TEXT,
    ip TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================
-- 5. 系统配置与统计缓存
-- ============================================
DROP TABLE IF EXISTS system_config CASCADE;
CREATE TABLE system_config (
    key VARCHAR(255) PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

DROP TABLE IF EXISTS stats_cache CASCADE;
CREATE TABLE stats_cache (
    key VARCHAR(255) PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================
-- 6. 同步与审计
-- ============================================
DROP TABLE IF EXISTS sync_log CASCADE;
CREATE TABLE sync_log (
    id SERIAL PRIMARY KEY,
    sync_date TEXT,
    data_date TEXT,
    file_name VARCHAR(500),
    records_count INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_sync_log_date ON sync_log(sync_date);
CREATE INDEX idx_sync_log_data_date ON sync_log(data_date);

DROP TABLE IF EXISTS sync_run CASCADE;
CREATE TABLE sync_run (
    id SERIAL PRIMARY KEY,
    sync_date TEXT,
    file_name VARCHAR(500),
    status VARCHAR(50),
    error_message TEXT,
    records_count INTEGER DEFAULT 0,
    invalid_records INTEGER DEFAULT 0,
    audit_records INTEGER DEFAULT 0,
    file_checksum VARCHAR(64),
    file_size BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_sync_run_date ON sync_run(sync_date);
CREATE INDEX idx_sync_run_status ON sync_run(status);

DROP TABLE IF EXISTS ingest_rejects CASCADE;
CREATE TABLE ingest_rejects (
    id SERIAL PRIMARY KEY,
    file_name TEXT,
    di_code TEXT,
    reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

DROP TABLE IF EXISTS data_audit_log CASCADE;
CREATE TABLE data_audit_log (
    id SERIAL PRIMARY KEY,
    di_code TEXT,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    source TEXT,
    file_name TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

DROP TABLE IF EXISTS sync_history CASCADE;
CREATE TABLE sync_history (
    id SERIAL PRIMARY KEY,
    sync_type TEXT,
    start_time TIMESTAMP WITH TIME ZONE,
    end_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    records_count INTEGER,
    status TEXT,
    message TEXT,
    duration_seconds INTEGER
);
CREATE INDEX idx_sync_history_start_time ON sync_history(start_time DESC);

-- ============================================
-- 7. 搜索日志
-- ============================================
DROP TABLE IF EXISTS search_logs CASCADE;
CREATE TABLE search_logs (
    id SERIAL PRIMARY KEY,
    query TEXT,
    query_type VARCHAR(50),
    results_count INTEGER,
    response_time_ms INTEGER,
    client_ip TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_search_logs_created ON search_logs(created_at);

-- ============================================
-- 8. 向量更新队列
-- ============================================
DROP TABLE IF EXISTS embedding_update_queue CASCADE;
CREATE TABLE embedding_update_queue (
    id SERIAL PRIMARY KEY,
    di_code VARCHAR(100) NOT NULL,
    action TEXT DEFAULT 'update',
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    claimed_at TIMESTAMP WITH TIME ZONE,
    processed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    UNIQUE(di_code, status)
);

CREATE INDEX idx_queue_status ON embedding_update_queue(status);
CREATE INDEX idx_queue_created ON embedding_update_queue(created_at);
CREATE INDEX idx_queue_claimed ON embedding_update_queue(claimed_at);

SELECT 'PostgreSQL schema initialized (runtime compatible)' AS status;
