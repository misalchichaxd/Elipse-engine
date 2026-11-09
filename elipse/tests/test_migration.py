"""
Fase 1 — Migración, llaves y poda por perfil.

Corre sin Ollama, sin Chroma y sin tu elipse.db real: crea una base con el ESQUEMA VIEJO en un
archivo temporal, le mete datos de prueba y comprueba que la migración no cambia nada de 'elipse'.

    pytest tests/test_migration.py -v
"""

import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.core import auth, db, profiles

# Esquema anterior a los perfiles (copia del init_db viejo).
LEGACY = """
CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL, creator TEXT NOT NULL, purpose TEXT NOT NULL, core_values TEXT NOT NULL);
CREATE TABLE traits (name TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE style_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE router_log (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL, provider_chosen TEXT NOT NULL, reason TEXT NOT NULL, duration_seconds REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE pending_actions (id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, arguments TEXT NOT NULL, detail TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pendiente', created_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT);
CREATE TABLE research_log (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, summary TEXT NOT NULL, memory_id TEXT, sources TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_used_at TEXT);
"""

TABLES = ("identity", "traits", "style_rules", "messages", "facts", "router_log",
          "pending_actions", "research_log", "api_keys")


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "elipse.db"
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(LEGACY)
    c.execute("INSERT INTO identity VALUES (1,'ELIPSE','Misael Urquidez','ayudar','honestidad')")
    c.execute("INSERT INTO traits VALUES ('formalidad', 0.3)")
    c.executemany("INSERT INTO messages (role, content) VALUES (?,?)", [("user", f"m{i}") for i in range(30)])
    c.executemany("INSERT INTO router_log (message, provider_chosen, reason, duration_seconds) VALUES (?,?,?,?)",
                  [(f"q{i}", "local", "sin tools", 1.0) for i in range(12)])
    for i in range(5):
        c.execute("INSERT INTO api_keys (name, key_hash) VALUES (?,?)", (f"llave{i}", f"hash{i}"))
    c.commit()
    c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _snapshot(path):
    """(tabla -> filas con solo las columnas originales)"""
    c = sqlite3.connect(path)
    out = {}
    for t in TABLES:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        orig = [x for x in cols if x not in ("profile", "session", "actor", "tools", "status", "caps")
                or (t, x) == ("pending_actions", "status")]
        out[t] = c.execute(f"SELECT {','.join(orig)} FROM {t} ORDER BY 1").fetchall()
    c.close()
    return out


def test_migracion_no_toca_datos_y_marca_todo_como_elipse(legacy_db):
    antes = _snapshot(legacy_db)
    db.init_db()
    assert _snapshot(legacy_db) == antes

    c = sqlite3.connect(legacy_db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    for t in ("messages", "facts", "router_log", "pending_actions", "api_keys"):
        assert c.execute(f"SELECT COUNT(*) FROM {t} WHERE profile != 'elipse'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM api_keys WHERE caps IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM router_log WHERE tools IS NOT NULL OR status IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
    c.close()


def test_migracion_idempotente(legacy_db):
    db.init_db()
    despues = _snapshot(legacy_db)
    db.init_db()
    db.init_db()
    assert _snapshot(legacy_db) == despues
    conn = db.get_connection()
    assert db.migrate(conn) is False      # ya está en la versión actual
    conn.close()


def test_respaldo_automatico_antes_de_migrar(legacy_db):
    db.init_db()
    backup = legacy_db.with_name("elipse.pre_perfiles.backup.db")
    assert backup.exists()
    c = sqlite3.connect(backup)
    assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 30
    assert "profile" not in [r[1] for r in c.execute("PRAGMA table_info(messages)")]   # copia ANTERIOR a migrar
    c.close()


def test_base_nueva_no_genera_respaldo(tmp_path, monkeypatch):
    path = tmp_path / "nueva.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    assert not path.with_name("nueva.pre_perfiles.backup.db").exists()
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    c.close()


def test_llaves_existentes_son_admin(legacy_db):
    db.init_db()
    llaves = auth.list_keys()
    assert len(llaves) == 5
    assert all(k["profile"] == "elipse" and k["caps"] is None for k in llaves)


def test_llave_admin_y_llave_de_anfitrion(legacy_db):
    db.init_db()
    admin = auth.create_key("panel")
    p = auth.resolve_principal(admin)
    assert p.is_admin and p.profile.is_default

    caps = {"tools": ["calculate"], "host_tools": True}
    host = auth.create_key("django", profile="rbe", caps=caps)
    h = auth.resolve_principal(host)
    assert not h.is_admin and h.profile.name == "rbe" and h.profile.caps == caps
    assert h.profile.synced is False          # el perfil aún no se sincronizó

    assert auth.resolve_principal("elp_inventada") is None
    assert auth.resolve_principal(None) is None


def test_endpoints_existentes_rechazan_llave_de_anfitrion(legacy_db):
    db.init_db()
    admin = auth.create_key("panel")
    host = auth.create_key("django", profile="rbe", caps={})

    assert auth.require_api_key(f"Bearer {admin}").is_admin
    with pytest.raises(HTTPException) as e:
        auth.require_api_key(f"Bearer {host}")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        auth.require_admin_key(f"Bearer {host}")
    assert e.value.status_code == 403
    assert auth.require_principal(f"Bearer {host}").profile.name == "rbe"   # lista blanca (Fase 2)
    assert auth._verify_raw_key(admin) is True
    assert auth._verify_raw_key(host) is False

    for malo in (None, "", "Bearer", "Token xyz", "Bearer elp_no_existe"):
        with pytest.raises(HTTPException) as e:
            auth.require_api_key(malo)
        assert e.value.status_code == 401


def test_llave_revocada_no_pasa(legacy_db):
    db.init_db()
    raw = auth.create_key("temporal")
    kid = auth.list_keys()[0]["id"]
    assert auth.revoke_key(kid)
    assert auth.resolve_principal(raw) is None


def test_create_key_valida_perfil_y_caps(legacy_db):
    db.init_db()
    for perfil in ("Elipse2", "a", "admin", "fish", "default", "rbe express", "1rbe"):
        with pytest.raises(ValueError):
            auth.create_key("x", profile=perfil, caps={})
    with pytest.raises(ValueError):
        auth.create_key("x", profile="rbe")                      # sin caps
    with pytest.raises(ValueError):
        auth.create_key("x", profile="elipse", caps={})          # elipse no lleva caps
    assert len(auth.list_keys()) == 5                            # nada se creó


def test_caps_rotos_fallan_cerrado(legacy_db):
    db.init_db()
    raw = auth.create_key("django", profile="rbe", caps={"tools": ["calculate"]})
    conn = db.get_connection()
    conn.execute("UPDATE api_keys SET caps = 'esto no es json' WHERE name = 'django'")
    conn.commit()
    conn.close()
    p = auth.resolve_principal(raw)
    assert p.profile.caps == {} and not p.is_admin               # techo vacío = todo apagado

    # una llave 'elipse' con caps (manipulación directa de la BD) NO se acepta
    raw2 = auth.create_key("manipulada")
    conn = db.get_connection()
    conn.execute("UPDATE api_keys SET caps = '{}' WHERE name = 'manipulada'")
    conn.commit()
    conn.close()
    assert auth.resolve_principal(raw2) is None


def test_perfil_desactivado_da_403(legacy_db):
    db.init_db()
    raw = auth.create_key("django", profile="rbe", caps={})
    conn = db.get_connection()
    conn.execute("INSERT INTO profiles (name, spec, enabled) VALUES ('rbe', '{}', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(HTTPException) as e:
        auth.require_principal(f"Bearer {raw}")
    assert e.value.status_code == 403
    assert profiles.get_profile("rbe") is None


def test_valid_name():
    assert profiles.valid_name("rbe") and profiles.valid_name("rbe-2_x")
    assert not profiles.valid_name("elipse") and not profiles.valid_name("RBE")
    assert not profiles.valid_name("") and not profiles.valid_name(None) and not profiles.valid_name("a" * 33)


def test_poda_por_perfil_no_desplaza_a_elipse(legacy_db):
    db.init_db()
    conn = db.get_connection()
    # 'elipse' ya tiene 30 messages y 12 router_log. 'rbe' mete muchísimos.
    conn.executemany("INSERT INTO messages (role, content, profile) VALUES ('user', ?, 'rbe')",
                     [(f"r{i}",) for i in range(300)])
    conn.executemany("INSERT INTO router_log (message, provider_chosen, reason, profile) VALUES (?, 'local', 'x', 'rbe')",
                     [(f"r{i}",) for i in range(300)])
    spec = {"retention": {"messages_max_rows": 50, "router_log_max_rows": 20}}
    conn.execute("INSERT INTO profiles (name, spec) VALUES ('rbe', ?)", (json.dumps(spec),))
    conn.commit()
    conn.close()

    db.prune_old_rows(keep_messages=25, keep_logs=10)   # topes chicos para probar

    conn = db.get_connection()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    assert q("SELECT COUNT(*) FROM messages WHERE profile='rbe'") == 50          # tope de su spec.retention
    assert q("SELECT COUNT(*) FROM router_log WHERE profile='rbe'") == 20
    assert q("SELECT COUNT(*) FROM messages WHERE profile='elipse'") == 25       # 30 filas, tope 25 (misma regla de siempre)
    assert q("SELECT COUNT(*) FROM router_log WHERE profile='elipse'") == 10
    conn.close()


def test_poda_elipse_identica_a_la_de_antes(legacy_db):
    """Con solo filas 'elipse', la poda nueva deja exactamente lo mismo que el DELETE viejo."""
    db.init_db()
    conn = db.get_connection()
    conn.executemany("INSERT INTO messages (role, content) VALUES ('user', ?)", [(f"n{i}",) for i in range(100)])
    conn.commit()
    ids_antes = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY id")]
    conn.close()
    esperado = [i for i in ids_antes if i > max(ids_antes) - 40]    # regla vieja: id > MAX(id) - keep

    db.prune_old_rows(keep_messages=40, keep_logs=500)
    conn = db.get_connection()
    assert [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY id")] == esperado
    conn.close()


def test_retencion_por_dias_solo_en_otros_perfiles(legacy_db):
    db.init_db()
    conn = db.get_connection()
    conn.execute("INSERT INTO messages (role, content, profile, created_at) VALUES ('user','viejo','rbe', datetime('now','-100 days'))")
    conn.execute("INSERT INTO messages (role, content, profile, created_at) VALUES ('user','nuevo','rbe', datetime('now','-1 days'))")
    conn.execute("UPDATE messages SET created_at = datetime('now','-400 days') WHERE profile='elipse'")   # 'elipse' NO caduca por días
    conn.execute("INSERT INTO profiles (name, spec) VALUES ('rbe', ?)", (json.dumps({"retention": {"messages_days": 90}}),))
    conn.commit()
    conn.close()
    db.prune_old_rows()
    conn = db.get_connection()
    assert [r[0] for r in conn.execute("SELECT content FROM messages WHERE profile='rbe'")] == ["nuevo"]
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE profile='elipse'").fetchone()[0] == 30
    conn.close()


def test_poda_con_retencion_corrupta_no_revienta(legacy_db):
    db.init_db()
    conn = db.get_connection()
    conn.execute("INSERT INTO messages (role, content, profile) VALUES ('user','x','rbe')")
    conn.execute("INSERT INTO profiles (name, spec) VALUES ('rbe', 'no es json')")
    conn.commit()
    conn.close()
    db.prune_old_rows()   # no debe lanzar