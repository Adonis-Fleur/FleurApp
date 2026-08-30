import os
import sqlite3

DB_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(DB_DIR, 'leanai.db')


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            name TEXT DEFAULT '',
            description TEXT DEFAULT '',
            avatar_path TEXT DEFAULT '',
            setup_skipped INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            personality TEXT DEFAULT '',
            avatar_path TEXT DEFAULT '',
            greeting TEXT DEFAULT '',
            visibility TEXT DEFAULT 'private',
            ignore_global_prompt INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id),
            label TEXT NOT NULL DEFAULT '',
            name TEXT DEFAULT '',
            description TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id INTEGER NOT NULL REFERENCES characters(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            file_path TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS ai_settings (
            user_id TEXT PRIMARY KEY REFERENCES users(id),
            context_length INTEGER DEFAULT 4096,
            temperature REAL DEFAULT 0.7,
            system_prompt TEXT DEFAULT '',
            llm_endpoint TEXT DEFAULT 'http://localhost:1234/v1',
            llm_model TEXT DEFAULT '',
            context_messages INTEGER DEFAULT 50,
            auto_extract INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS character_state (
            character_id INTEGER PRIMARY KEY REFERENCES characters(id),
            location TEXT DEFAULT '',
            clothes TEXT DEFAULT '',
            last_scan_msg_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id INTEGER NOT NULL REFERENCES characters(id),
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS npcs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id INTEGER NOT NULL REFERENCES characters(id),
            name TEXT NOT NULL,
            personality TEXT DEFAULT '',
            relationship TEXT DEFAULT '',
            avatar_path TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS image_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id),
            prompt TEXT NOT NULL,
            negative_prompt TEXT DEFAULT '',
            model_name TEXT DEFAULT '',
            settings_json TEXT DEFAULT '{}',
            image_path TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    # Migrate existing tables — add columns that may be missing
    for mig in [
        "ALTER TABLE ai_settings ADD COLUMN context_messages INTEGER DEFAULT 50",
        "ALTER TABLE ai_settings ADD COLUMN auto_extract INTEGER DEFAULT 1",
        "ALTER TABLE ai_settings ADD COLUMN auto_extract_interval INTEGER DEFAULT 10",
        "ALTER TABLE ai_settings ADD COLUMN streaming INTEGER DEFAULT 0",
        "ALTER TABLE ai_settings ADD COLUMN stream_speed INTEGER DEFAULT 0",
        "ALTER TABLE character_state ADD COLUMN last_scan_msg_id INTEGER DEFAULT 0",
        "ALTER TABLE messages ADD COLUMN speaker TEXT DEFAULT ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_npcs_char_name ON npcs(character_id, name)",
    ]:
        try:
            conn.execute(mig)
        except Exception:
            pass

    conn.commit()
    conn.close()


# ─── Users ───

def create_user(uid, password_hash):
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO users (id, password_hash) VALUES (?, ?)",
            (uid, password_hash),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_user(uid):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?", (uid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_user_profile(uid, name, description):
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET name = ?, description = ?, setup_skipped = 0 WHERE id = ?",
        (name, description, uid),
    )
    conn.commit()
    conn.close()


def update_user_avatar(uid, avatar_path):
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET avatar_path = ? WHERE id = ?",
        (avatar_path, uid),
    )
    conn.commit()
    conn.close()


def update_user_id(old_uid, new_uid, password_hash):
    conn = _get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE users SET id = ?, password_hash = ? WHERE id = ?",
                     (new_uid, password_hash, old_uid))
        conn.execute("UPDATE characters SET user_id = ? WHERE user_id = ?",
                     (new_uid, old_uid))
        conn.execute("UPDATE presets SET user_id = ? WHERE user_id = ?",
                     (new_uid, old_uid))
        conn.execute("UPDATE ai_settings SET user_id = ? WHERE user_id = ?",
                     (new_uid, old_uid))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def update_user_password(uid, password_hash):
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, uid),
    )
    conn.commit()
    conn.close()


def skip_user_setup(uid):
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET setup_skipped = 1 WHERE id = ?", (uid,)
    )
    conn.commit()
    conn.close()


# ─── Presets ───

def create_preset(user_id, label, name, description):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO presets (user_id, label, name, description) VALUES (?, ?, ?, ?)",
        (user_id, label, name, description),
    )
    conn.commit()
    conn.close()


def get_user_presets(user_id):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM presets WHERE user_id = ? ORDER BY label", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_preset(preset_id, user_id):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM presets WHERE id = ? AND user_id = ?",
        (preset_id, user_id),
    )
    conn.commit()
    conn.close()


# ─── Characters ───

def create_character(user_id, name, personality, greeting='', ignore_global_prompt=0):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO characters (user_id, name, personality, greeting, ignore_global_prompt) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, personality, greeting, ignore_global_prompt),
    )
    conn.commit()
    char_id = cur.lastrowid
    conn.close()
    return char_id


def get_user_characters(user_id):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM characters WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_character(char_id, user_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM characters WHERE id = ? AND user_id = ?",
        (char_id, user_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_character_avatar(char_id, avatar_path):
    conn = _get_conn()
    conn.execute(
        "UPDATE characters SET avatar_path = ? WHERE id = ?",
        (avatar_path, char_id),
    )
    conn.commit()
    conn.close()


def update_character(char_id, user_id, name, personality, greeting, ignore_global_prompt):
    conn = _get_conn()
    conn.execute(
        "UPDATE characters SET name=?, personality=?, greeting=?, ignore_global_prompt=? WHERE id=? AND user_id=?",
        (name, personality, greeting, ignore_global_prompt, char_id, user_id),
    )
    conn.commit()
    conn.close()


def delete_character(char_id, user_id):
    conn = _get_conn()
    conn.execute("DELETE FROM npcs WHERE character_id = ?", (char_id,))
    conn.execute("DELETE FROM messages WHERE character_id = ?", (char_id,))
    conn.execute("DELETE FROM memories WHERE character_id = ?", (char_id,))
    conn.execute("DELETE FROM character_state WHERE character_id = ?", (char_id,))
    conn.execute("DELETE FROM characters WHERE id = ? AND user_id = ?", (char_id, user_id))
    conn.commit()
    conn.close()


def duplicate_character(char_id, user_id):
    conn = _get_conn()
    orig = conn.execute(
        "SELECT * FROM characters WHERE id = ? AND user_id = ?", (char_id, user_id)
    ).fetchone()
    if not orig:
        conn.close()
        return None
    cur = conn.execute(
        "INSERT INTO characters (user_id, name, personality, avatar_path, greeting, visibility, ignore_global_prompt) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (orig['user_id'], orig['name'] + ' (copy)', orig['personality'], orig['avatar_path'], orig['greeting'], orig['visibility'], orig['ignore_global_prompt']),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


# ─── Messages ───

def create_message(character_id, role, content, file_path='', speaker=''):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO messages (character_id, role, content, file_path, speaker) VALUES (?, ?, ?, ?, ?)",
        (character_id, role, content, file_path, speaker),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return msg_id


def get_character_messages(character_id):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE character_id = ? ORDER BY created_at",
        (character_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_character_messages(character_id, user_id):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM messages WHERE character_id = ? AND character_id IN "
        "(SELECT id FROM characters WHERE id = ? AND user_id = ?)",
        (character_id, character_id, user_id),
    )
    conn.commit()
    conn.close()


def update_message(message_id, content):
    conn = _get_conn()
    conn.execute(
        "UPDATE messages SET content = ? WHERE id = ?",
        (content, message_id),
    )
    conn.commit()
    conn.close()


def delete_message(message_id, character_id):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM messages WHERE id = ? AND character_id = ?",
        (message_id, character_id),
    )
    conn.commit()
    conn.close()


def delete_messages_from(message_id, character_id):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM messages WHERE character_id = ? AND id >= ?",
        (character_id, message_id),
    )
    conn.commit()
    conn.close()


def get_message(message_id, character_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ? AND character_id = ?",
        (message_id, character_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── AI Settings ───

def get_ai_settings(user_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM ai_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        'user_id': user_id,
        'context_length': 4096,
        'temperature': 0.7,
        'system_prompt': '',
        'llm_endpoint': 'http://localhost:1234/v1',
        'llm_model': '',
        'context_messages': 50,
        'auto_extract': 1,
        'auto_extract_interval': 10,
        'streaming': 0,
        'stream_speed': 0,
    }


def save_ai_settings(user_id, context_length, temperature, system_prompt, llm_endpoint, llm_model, context_messages, auto_extract, auto_extract_interval=10, streaming=0, stream_speed=0):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO ai_settings (user_id, context_length, temperature, system_prompt, llm_endpoint, llm_model, context_messages, auto_extract, auto_extract_interval, streaming, stream_speed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            context_length=excluded.context_length,
            temperature=excluded.temperature,
            system_prompt=excluded.system_prompt,
            llm_endpoint=excluded.llm_endpoint,
            llm_model=excluded.llm_model,
            context_messages=excluded.context_messages,
            auto_extract=excluded.auto_extract,
            auto_extract_interval=excluded.auto_extract_interval,
            streaming=excluded.streaming,
            stream_speed=excluded.stream_speed
    """, (user_id, context_length, temperature, system_prompt, llm_endpoint, llm_model, context_messages, auto_extract, auto_extract_interval, streaming, stream_speed))
    conn.commit()
    conn.close()


# ─── Character State ───

def get_character_state(character_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM character_state WHERE character_id = ?", (character_id,)
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {'character_id': character_id, 'location': '', 'clothes': '', 'last_scan_msg_id': 0}


def save_character_state(character_id, location, clothes, last_scan_msg_id=None):
    conn = _get_conn()
    if last_scan_msg_id is not None:
        conn.execute("""
            INSERT INTO character_state (character_id, location, clothes, last_scan_msg_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                location=excluded.location, clothes=excluded.clothes, last_scan_msg_id=excluded.last_scan_msg_id
        """, (character_id, location, clothes, last_scan_msg_id))
    else:
        conn.execute("""
            INSERT INTO character_state (character_id, location, clothes)
            VALUES (?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                location=excluded.location, clothes=excluded.clothes
        """, (character_id, location, clothes))
    conn.commit()
    conn.close()


# ─── Memories ───

def _normalize_mem(s: str) -> str:
    import re
    s = s.strip().lower().rstrip('.!?,;')
    s = re.sub(r'\s+', ' ', s)
    return s

def add_memory(character_id, content):
    content = (content or '').strip()
    if not content:
        return False
    # normalized dedup: fetch existing for this character and compare
    conn = _get_conn()
    rows = conn.execute(
        "SELECT content FROM memories WHERE character_id = ?", (character_id,)
    ).fetchall()
    norm_new = _normalize_mem(content)
    for r in rows:
        if _normalize_mem(r['content']) == norm_new:
            conn.close()
            return False
        # also jaccard near-dup (>=0.88)
        a = set(norm_new.split())
        b = set(_normalize_mem(r['content']).split())
        if a and b:
            inter = len(a & b)
            union = len(a | b)
            if union and inter / union >= 0.88:
                conn.close()
                return False
    conn.execute(
        "INSERT INTO memories (character_id, content) VALUES (?, ?)",
        (character_id, content),
    )
    conn.commit()
    conn.close()
    return True


def get_memories(character_id, limit=50):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE character_id = ? ORDER BY created_at DESC LIMIT ?",
        (character_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def clear_memories(character_id):
    conn = _get_conn()
    conn.execute("DELETE FROM memories WHERE character_id = ?", (character_id,))
    conn.commit()
    conn.close()


# ─── NPCs ───

def add_npc(character_id, name, personality='', relationship=''):
    conn = _get_conn()
    dup = conn.execute(
        "SELECT 1 FROM npcs WHERE character_id = ? AND name = ? COLLATE NOCASE LIMIT 1",
        (character_id, name),
    ).fetchone()
    if dup:
        row = conn.execute(
            "SELECT * FROM npcs WHERE character_id = ? AND name = ? COLLATE NOCASE",
            (character_id, name),
        ).fetchone()
        conn.close()
        return dict(row)
    cur = conn.execute(
        "INSERT INTO npcs (character_id, name, personality, relationship, is_active) VALUES (?, ?, ?, ?, 1)",
        (character_id, name, personality, relationship),
    )
    conn.commit()
    npc_id = cur.lastrowid
    row = conn.execute("SELECT * FROM npcs WHERE id = ?", (npc_id,)).fetchone()
    conn.close()
    return dict(row)


def get_character_npcs(character_id):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM npcs WHERE character_id = ? ORDER BY created_at",
        (character_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_npcs(character_id):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM npcs WHERE character_id = ? AND is_active = 1 ORDER BY created_at",
        (character_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_npc(npc_id, character_id, name=None, personality=None, relationship=None, is_active=None):
    conn = _get_conn()
    fields = []
    vals = []
    if name is not None:
        fields.append("name = ?")
        vals.append(name)
    if personality is not None:
        fields.append("personality = ?")
        vals.append(personality)
    if relationship is not None:
        fields.append("relationship = ?")
        vals.append(relationship)
    if is_active is not None:
        fields.append("is_active = ?")
        vals.append(1 if is_active else 0)
    if not fields:
        conn.close()
        return
    vals.extend([npc_id, character_id])
    conn.execute(
        f"UPDATE npcs SET {', '.join(fields)} WHERE id = ? AND character_id = ?",
        vals,
    )
    conn.commit()
    conn.close()


def set_npc_active(npc_id, character_id, is_active):
    conn = _get_conn()
    conn.execute(
        "UPDATE npcs SET is_active = ? WHERE id = ? AND character_id = ?",
        (1 if is_active else 0, npc_id, character_id),
    )
    conn.commit()
    conn.close()


def delete_npc(npc_id, character_id):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM npcs WHERE id = ? AND character_id = ?",
        (npc_id, character_id),
    )
    conn.commit()
    conn.close()


# ─── Image History ───


def add_generated_image(user_id, prompt, negative_prompt, model_name, settings_json, image_path):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO image_history (user_id, prompt, negative_prompt, model_name, settings_json, image_path) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, prompt, negative_prompt, model_name, settings_json, image_path),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def get_image_history(user_id, limit=50):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM image_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_generated_image(image_id, user_id):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM image_history WHERE id = ? AND user_id = ?",
        (image_id, user_id),
    )
    conn.commit()
    conn.close()


init_db()
