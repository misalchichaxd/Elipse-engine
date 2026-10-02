import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "elipse.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_connection()
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
    conn.close()