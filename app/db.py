"""PostgreSQL 连接池管理（psycopg3）。"""
import logging
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger("caltrace")

_pool: ConnectionPool | None = None

# 幂等结构补丁：为沿用旧数据卷的部署补充后续版本新增的表。
# 与 db/init.sql 中同名表定义保持一致（全新数据卷由 init.sql 建表，此处为 no-op）。
_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS issue_batches (
    batch_key    TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request      JSONB NOT NULL,
    response     JSONB NOT NULL,
    cert_nos     TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 证书登记更正声明（展示资料更正，只增不改；不触碰 certificates 封印行）
CREATE OR REPLACE FUNCTION cert_corrections_changes_valid(changes JSONB)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    e record;
BEGIN
    IF jsonb_typeof(changes) IS DISTINCT FROM 'object' OR changes = '{}'::jsonb THEN
        RETURN false;
    END IF;
    FOR e IN SELECT key, value FROM jsonb_each(changes) LOOP
        IF e.key NOT IN ('device_name', 'operator', 'remark') THEN
            RETURN false;
        END IF;
        -- jsonb 中的 null 是合法值（显式置空）；标量变量需用 IS DISTINCT FROM 判空
        IF e.value IS DISTINCT FROM 'null'::jsonb
           AND jsonb_typeof(e.value) <> 'string' THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

CREATE TABLE IF NOT EXISTS cert_corrections (
    statement_no TEXT PRIMARY KEY,
    cert_no      TEXT NOT NULL REFERENCES certificates(cert_no),
    seq          INT  NOT NULL,
    changes      JSONB NOT NULL,
    reason       TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response     JSONB NOT NULL,
    declared_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cert_corrections_seq_positive CHECK (seq > 0),
    CONSTRAINT cert_corrections_changes_shape
        CHECK (cert_corrections_changes_valid(changes)),
    CONSTRAINT cert_corrections_reason_nonempty CHECK (length(btrim(reason)) > 0),
    CONSTRAINT cert_corrections_seq_unique UNIQUE (cert_no, seq)
);
CREATE INDEX IF NOT EXISTS idx_cert_corrections_cert ON cert_corrections(cert_no, seq);

CREATE OR REPLACE FUNCTION cert_corrections_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'correction statement % is sealed: declarations cannot be modified',
        OLD.statement_no
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cert_corrections_immutable_update') THEN
        CREATE TRIGGER trg_cert_corrections_immutable_update
            BEFORE UPDATE ON cert_corrections
            FOR EACH ROW EXECUTE FUNCTION cert_corrections_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cert_corrections_immutable_delete') THEN
        CREATE TRIGGER trg_cert_corrections_immutable_delete
            BEFORE DELETE ON cert_corrections
            FOR EACH ROW EXECUTE FUNCTION cert_corrections_immutable();
    END IF;
END
$$;
"""


def _build_dsn() -> str:
    return (
        f"host={settings.db_host} port={settings.db_port} "
        f"dbname={settings.db_name} user={settings.db_user} "
        f"password={settings.db_password}"
    )


def init_pool() -> ConnectionPool:
    """创建连接池，并在数据库尚未就绪时重试等待。"""
    global _pool
    if _pool is not None:
        return _pool

    deadline = time.monotonic() + settings.db_connect_timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool = ConnectionPool(
                _build_dsn(),
                min_size=settings.db_min_size,
                max_size=settings.db_max_size,
                kwargs={"row_factory": dict_row},
                open=True,
                timeout=10,
            )
            # 确认连接真正可用
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _pool = pool
            logger.info("database connection pool ready")
            return pool
        except Exception as exc:  # 数据库容器可能仍在初始化
            last_err = exc
            logger.warning("waiting for database: %s", exc)
            time.sleep(2)
    raise RuntimeError(f"database not ready after {settings.db_connect_timeout}s: {last_err}")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_schema(pool: ConnectionPool) -> None:
    """启动时执行幂等结构补丁（不改动既有表与数据）。"""
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(_MIGRATION_SQL)
    logger.info("schema migration ensured")


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not initialized")
    return _pool
