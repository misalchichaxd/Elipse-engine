import json
import os
import sqlite3
from pathlib import Path

# ELIPSE_DB_PATH permite apuntar a otra base (tests, copias de trabajo).
# Sin la variable, la ruta de siempre: <raíz del proyecto>/elipse.db
DB_PATH = Path(os.environ.get("ELIPSE_DB_PATH") or Path(__file__).parent.parent.parent / "elipse.db")

# Versión del esquema (PRAGMA user_version). 0 = base anterior a los perfiles.
SCHEMA_VERSION = 1


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


# ---- Poda (R11: por perfil) ----

def _as_int(value, default: int) -> int:
    try:
        n = int(value)
        return n if n >= 0 else default
    except (TypeError, ValueError):
        return default


def _retention(conn, profile: str) -> dict:
    """spec.retention del perfil (vacío si no existe o el JSON está mal)."""
    row = conn.execute("SELECT spec FROM profiles WHERE name = ?", (profile,)).fetchone()
    if not row:
        return {}
    try:
        spec = json.loads(row["spec"])
        ret = spec.get("retention") if isinstance(spec, dict) else None
        return ret if isinstance(ret, dict) else {}
    except (TypeError, ValueError):
        return {}


def _columns(conn, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def prune_old_rows(keep_messages: int = 2000, keep_logs: int = 500) -> None:
    """Mantiene acotadas las tablas que solo crecen sin alterar lecturas actuales.

    Por perfil: 'elipse' conserva exactamente los topes de siempre; los demás perfiles se
    podan solo entre sí (tope y días de spec.retention) y nunca desplazan filas de 'elipse'.
    """
    conn = get_connection()
    try:
        for table, keep in (("messages", keep_messages), ("router_log", keep_logs)):
            if "profile" not in _columns(conn, table):
                # Base sin migrar: poda global de siempre.
                conn.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id) FROM {table}) - ?", (keep,))
                continue
            for (prof,) in conn.execute(f"SELECT DISTINCT profile FROM {table}").fetchall():
                limit = keep
                if prof != "elipse":
                    ret = _retention(conn, prof)
                    limit = _as_int(ret.get(f"{table}_max_rows"), keep)
                    days = _as_int(ret.get(f"{table}_days"), 0)
                    if days:
                        conn.execute(
                            f"DELETE FROM {table} WHERE profile = ? AND created_at < datetime('now', ?)",
                            (prof, f"-{days} days"),
                        )
                conn.execute(
                    f"DELETE FROM {table} WHERE profile = ? AND id <= "
                    f"(SELECT MAX(id) FROM {table} WHERE profile = ?) - ?",
                    (prof, prof, limit),
                )
        # research_log no tiene perfil (investigación apagada fuera de 'elipse'): poda global como siempre.
        conn.execute("DELETE FROM research_log WHERE id <= (SELECT MAX(id) FROM research_log) - ?", (keep_logs,))
        conn.commit()
    finally:
        conn.close()


# ---- Migración (R2: solo aditiva) ----

# (tabla, columna, definición). Todo lo existente queda como 'elipse' por el DEFAULT.
_NEW_COLUMNS = (
    ("messages", "profile", "TEXT NOT NULL DEFAULT 'elipse'"),
    ("messages", "session", "TEXT NOT NULL DEFAULT ''"),
    ("facts", "profile", "TEXT NOT NULL DEFAULT 'elipse'"),
    ("router_log", "profile", "TEXT NOT NULL DEFAULT 'elipse'"),
    ("router_log", "actor", "TEXT NOT NULL DEFAULT ''"),
    ("router_log", "tools", "TEXT"),    # JSON array; NULL en filas viejas
    ("router_log", "status", "TEXT"),   # ok | error | cancelado | pendiente; NULL en viejas
    ("pending_actions", "profile", "TEXT NOT NULL DEFAULT 'elipse'"),
    ("api_keys", "profile", "TEXT NOT NULL DEFAULT 'elipse'"),
    ("api_keys", "caps", "TEXT"),       # JSON; NULL = sin techo (llaves actuales = admin)
)

_PROFILES_DDL = """
    CREATE TABLE IF NOT EXISTS profiles (
        name            TEXT PRIMARY KEY,
        spec            TEXT NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        callback_url    TEXT,
        callback_secret TEXT,
        created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
    )
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_profile   ON messages(profile, session, id)",
    "CREATE INDEX IF NOT EXISTS idx_router_log_profile ON router_log(profile, id)",
    "CREATE INDEX IF NOT EXISTS idx_pending_profile    ON pending_actions(profile, status)",
)


def _has_user_data(conn) -> bool:
    return bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM api_keys) OR EXISTS(SELECT 1 FROM messages) OR EXISTS(SELECT 1 FROM identity)"
    ).fetchone()[0])


def _backup_before_migrate(conn) -> None:
    """Copia consistente (sirve con WAL) antes de tocar una base con datos. No pisa una anterior."""
    dest = DB_PATH.with_name(DB_PATH.stem + ".pre_perfiles.backup.db")
    if dest.exists():
        return
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()


def migrate(conn) -> bool:
    """Lleva la base a SCHEMA_VERSION. Idempotente. Devuelve True si aplicó algo."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return False
    conn.commit()
    if _has_user_data(conn):
        _backup_before_migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, column, ddl in _NEW_COLUMNS:
            if column not in _columns(conn, table):   # se mira dentro de la transacción
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute(_PROFILES_DDL)
        for stmt in _INDEXES:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def init_db():
    conn = get_connection()
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS identity (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            creator TEXT NOT NULL,
            purpose TEXT NOT NULL,
            core_values TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS traits (
            name TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS style_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS router_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        provider_chosen TEXT NOT NULL,
        reason TEXT NOT NULL,
        duration_seconds REAL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_actions (
        id TEXT PRIMARY KEY,
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        detail TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pendiente',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT
    )
""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS research_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        summary TEXT NOT NULL,
        memory_id TEXT,
        sources TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_used_at TEXT
    )
""")
    conn.commit()
    migrate(conn)          # tras crear las tablas y ANTES de podar (las columnas nuevas deben existir)
    conn.close()
    prune_old_rows()