"""
数据库封装
支持两种模式：
  - sqlite: 本地 SQLite 数据库（默认，本地部署推荐）
  - upstash: Upstash Redis（云端部署用）
通过 config.DB_TYPE 切换
"""

import json
import time
import sqlite3
import threading
import os
import config


# ============================================================
# SQLite 本地数据库
# ============================================================
class SQLiteDB:
    """SQLite 本地数据库，模拟 Redis 常用操作接口"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        self.enabled = True
        self._init_tables()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS kv_store (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        expire_at INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS set_store (
                        set_name TEXT,
                        member TEXT,
                        PRIMARY KEY (set_name, member)
                    );
                    CREATE TABLE IF NOT EXISTS hash_store (
                        hash_name TEXT,
                        field TEXT,
                        value TEXT,
                        PRIMARY KEY (hash_name, field)
                    );
                    CREATE INDEX IF NOT EXISTS idx_kv_expire ON kv_store(expire_at);
                    CREATE INDEX IF NOT EXISTS idx_set_name ON set_store(set_name);
                    CREATE INDEX IF NOT EXISTS idx_hash_name ON hash_store(hash_name);
                """)
                conn.commit()
            finally:
                conn.close()

    def _clean_expired(self, conn):
        """清理过期的 key"""
        now = int(time.time())
        conn.execute("DELETE FROM kv_store WHERE expire_at > 0 AND expire_at <= ?", (now,))

    def _exec(self, *args):
        """模拟 Redis 命令执行"""
        if not args:
            return None
        cmd = args[0].upper()
        with self._lock:
            conn = self._get_conn()
            try:
                self._clean_expired(conn)

                if cmd == "GET":
                    key = args[1]
                    row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
                    return row["value"] if row else None

                elif cmd == "SET":
                    key = args[1]
                    value = str(args[2])
                    expire_at = 0
                    if len(args) > 3 and args[3].upper() == "EX":
                        expire_at = int(time.time()) + int(args[4])
                    conn.execute(
                        "INSERT OR REPLACE INTO kv_store (key, value, expire_at) VALUES (?, ?, ?)",
                        (key, value, expire_at)
                    )
                    conn.commit()
                    return "OK"

                elif cmd == "DEL":
                    key = args[1]
                    cur = conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))
                    conn.execute("DELETE FROM set_store WHERE set_name = ?", (key,))
                    conn.execute("DELETE FROM hash_store WHERE hash_name = ?", (key,))
                    conn.commit()
                    return cur.rowcount

                elif cmd == "EXISTS":
                    key = args[1]
                    row = conn.execute("SELECT 1 FROM kv_store WHERE key = ?", (key,)).fetchone()
                    if row:
                        return 1
                    row = conn.execute("SELECT 1 FROM set_store WHERE set_name = ? LIMIT 1", (key,)).fetchone()
                    if row:
                        return 1
                    row = conn.execute("SELECT 1 FROM hash_store WHERE hash_name = ? LIMIT 1", (key,)).fetchone()
                    return 1 if row else 0

                elif cmd == "EXPIRE":
                    key = args[1]
                    seconds = int(args[2])
                    expire_at = int(time.time()) + seconds
                    conn.execute("UPDATE kv_store SET expire_at = ? WHERE key = ?", (expire_at, key))
                    conn.commit()
                    return 1

                elif cmd == "SADD":
                    set_name = args[1]
                    added = 0
                    for member in args[2:]:
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO set_store (set_name, member) VALUES (?, ?)",
                                (set_name, str(member))
                            )
                            if conn.total_changes > 0:
                                added += 1
                        except Exception:
                            pass
                    conn.commit()
                    return added

                elif cmd == "SREM":
                    set_name = args[1]
                    removed = 0
                    for member in args[2:]:
                        cur = conn.execute(
                            "DELETE FROM set_store WHERE set_name = ? AND member = ?",
                            (set_name, str(member))
                        )
                        removed += cur.rowcount
                    conn.commit()
                    return removed

                elif cmd == "SMEMBERS":
                    set_name = args[1]
                    rows = conn.execute(
                        "SELECT member FROM set_store WHERE set_name = ?", (set_name,)
                    ).fetchall()
                    return [row["member"] for row in rows]

                elif cmd == "SISMEMBER":
                    set_name = args[1]
                    member = str(args[2])
                    row = conn.execute(
                        "SELECT 1 FROM set_store WHERE set_name = ? AND member = ?",
                        (set_name, member)
                    ).fetchone()
                    return 1 if row else 0

                elif cmd == "HINCRBY":
                    hash_name = args[1]
                    field = args[2]
                    increment = int(args[3])
                    row = conn.execute(
                        "SELECT value FROM hash_store WHERE hash_name = ? AND field = ?",
                        (hash_name, field)
                    ).fetchone()
                    current = int(row["value"]) if row else 0
                    new_val = current + increment
                    conn.execute(
                        "INSERT OR REPLACE INTO hash_store (hash_name, field, value) VALUES (?, ?, ?)",
                        (hash_name, field, str(new_val))
                    )
                    conn.commit()
                    return new_val

                elif cmd == "HGETALL":
                    hash_name = args[1]
                    rows = conn.execute(
                        "SELECT field, value FROM hash_store WHERE hash_name = ?", (hash_name,)
                    ).fetchall()
                    result = []
                    for row in rows:
                        result.append(row["field"])
                        result.append(row["value"])
                    return result

                elif cmd == "MGET":
                    keys = args[1:]
                    result = []
                    for key in keys:
                        row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
                        result.append(row["value"] if row else None)
                    return result

                elif cmd == "KEYS":
                    pattern = args[1]
                    like_pattern = pattern.replace("*", "%").replace("?", "_")
                    rows = conn.execute(
                        "SELECT key FROM kv_store WHERE key LIKE ?", (like_pattern,)
                    ).fetchall()
                    return [row["key"] for row in rows]

                else:
                    print(f"[SQLite] 未实现的命令: {cmd}")
                    return None

            except Exception as e:
                print(f"[SQLite] 命令失败 {cmd}: {e}")
                return None
            finally:
                conn.close()

    def add_user(self, user_id: int):
        self._exec("SADD", "bot:users", str(user_id))

    def get_users(self) -> list:
        result = self._exec("SMEMBERS", "bot:users")
        return result if result else []

    def ban_user(self, user_id):
        self._exec("SADD", "bot:banned", str(user_id))

    def unban_user(self, user_id):
        self._exec("SREM", "bot:banned", str(user_id))

    def get_banned(self) -> list:
        result = self._exec("SMEMBERS", "bot:banned")
        return result if result else []

    def is_banned(self, user_id: int) -> bool:
        result = self._exec("SISMEMBER", "bot:banned", str(user_id))
        return bool(result)

    def get_welcome(self) -> str:
        result = self._exec("GET", "bot:welcome")
        return result if result else ""

    def set_welcome(self, text: str):
        self._exec("SET", "bot:welcome", text)

    def reset_welcome(self):
        self._exec("DEL", "bot:welcome")

    def incr_search(self):
        self._exec("HINCRBY", "bot:stats", "total_searches", 1)

    def incr_play(self):
        self._exec("HINCRBY", "bot:stats", "total_plays", 1)

    def get_stats(self) -> dict:
        result = self._exec("HGETALL", "bot:stats")
        if result and isinstance(result, list):
            return {result[i]: int(result[i + 1]) for i in range(0, len(result), 2)}
        return {"total_searches": 0, "total_plays": 0}

    def get_cookie(self) -> str:
        result = self._exec("GET", "bot:cookie")
        return result if result else ""

    def set_cookie(self, cookie: str):
        self._exec("SET", "bot:cookie", cookie)
        self._exec("SET", "bot:cookie_updated_at", str(int(time.time())))

    def get_cookie_updated_at(self) -> int:
        result = self._exec("GET", "bot:cookie_updated_at")
        return int(result) if result else 0

    def get_quality(self) -> str:
        result = self._exec("GET", "bot:quality")
        return result if result else "standard"

    def set_quality(self, quality: str):
        self._exec("SET", "bot:quality", quality)

    def get_last_cookie_check(self) -> int:
        result = self._exec("GET", "bot:last_cookie_check")
        return int(result) if result else 0

    def set_last_cookie_check(self, ts: int):
        self._exec("SET", "bot:last_cookie_check", str(ts))

    def save_active_playlist(self, user_id: int, playlist_id: int, songs: list, current_index: int = 0):
        data = {
            "playlist_id": playlist_id,
            "current_index": current_index,
            "total": len(songs),
            "songs": songs,
            "start_time": int(time.time())
        }
        self._exec("SET", f"playlist:active:{user_id}", json.dumps(data, ensure_ascii=False), "EX", 86400)
        self._exec("SADD", "playlist:active_users", str(user_id))

    def get_active_playlist(self, user_id: int) -> dict:
        result = self._exec("GET", f"playlist:active:{user_id}")
        if not result:
            return {}
        try:
            return json.loads(result)
        except Exception:
            return {}

    def update_playlist_index(self, user_id: int, current_index: int):
        data = self.get_active_playlist(user_id)
        if not data:
            return
        data["current_index"] = current_index
        self._exec("SET", f"playlist:active:{user_id}", json.dumps(data, ensure_ascii=False), "EX", 86400)

    def remove_active_playlist(self, user_id: int):
        self._exec("DEL", f"playlist:active:{user_id}")
        self._exec("SREM", "playlist:active_users", str(user_id))

    def get_active_playlist_users(self) -> list:
        result = self._exec("SMEMBERS", "playlist:active_users")
        return [int(x) for x in result] if result else []

    def set_playlist_stop_flag(self, user_id: int):
        self._exec("SET", f"playlist:stop:{user_id}", "1", "EX", 60)

    def check_playlist_stop_flag(self, user_id: int) -> bool:
        exists = self._exec("EXISTS", f"playlist:stop:{user_id}")
        if exists:
            self._exec("DEL", f"playlist:stop:{user_id}")
            return True
        return False

    def get_file_id(self, song_id: int) -> str:
        result = self._exec("GET", f"cache:file_id:{song_id}")
        return result if result else ""

    def get_file_ids_batch(self, song_ids: list) -> dict:
        if not song_ids:
            return {}
        keys = [f"cache:file_id:{sid}" for sid in song_ids]
        results = self._exec("MGET", *keys)
        if not results:
            return {sid: "" for sid in song_ids}
        return {sid: (results[i] if i < len(results) and results[i] else "") for i, sid in enumerate(song_ids)}

    def set_file_id(self, song_id: int, file_id: str):
        self._exec("SET", f"cache:file_id:{song_id}", file_id)

    def add_searched_song(self, song_id: int):
        self._exec("SADD", "cache:searched_songs", str(song_id))

    def get_uncached_searched_songs(self, limit: int = 100) -> list:
        all_searched = self._exec("SMEMBERS", "cache:searched_songs") or []
        uncached = []
        for sid_str in all_searched:
            try:
                sid = int(sid_str)
                if not self.get_file_id(sid):
                    uncached.append(sid)
                    if len(uncached) >= limit:
                        break
            except (ValueError, TypeError):
                continue
        return uncached

    def clear_all_file_ids(self) -> int:
        keys = self._exec("KEYS", "cache:file_id:*")
        if not keys:
            return 0
        count = 0
        for k in keys:
            self._exec("DEL", k)
            count += 1
        return count

    def get_admins(self) -> list:
        result = self._exec("SMEMBERS", "bot:admins")
        return [int(x) for x in result] if result else []

    def add_admin(self, user_id: int):
        self._exec("SADD", "bot:admins", str(user_id))

    def remove_admin(self, user_id: int):
        self._exec("SREM", "bot:admins", str(user_id))

    def is_admin(self, user_id: int) -> bool:
        if user_id == config.ADMIN_ID:
            return True
        result = self._exec("SISMEMBER", "bot:admins", str(user_id))
        return bool(result)


# ============================================================
# Upstash Redis 数据库（云端部署用）
# ============================================================
class UpstashDB:
    """Upstash Redis 数据库操作"""

    def __init__(self):
        import requests
        self._requests = requests
        self.url = config.UPSTASH_REDIS_REST_URL.rstrip("/")
        self.token = config.UPSTASH_REDIS_REST_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.enabled = bool(self.url and self.token)

    def _exec(self, *args):
        if not self.enabled:
            return None
        try:
            payload = json.dumps(list(args))
            resp = self._requests.post(
                self.url, data=payload, headers=self.headers, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result")
        except Exception as e:
            print(f"[Upstash] 命令失败 {args[0]}: {e}")
            return None

    def add_user(self, user_id: int):
        self._exec("SADD", "bot:users", str(user_id))

    def get_users(self) -> list:
        result = self._exec("SMEMBERS", "bot:users")
        return result if result else []

    def ban_user(self, user_id):
        self._exec("SADD", "bot:banned", str(user_id))

    def unban_user(self, user_id):
        self._exec("SREM", "bot:banned", str(user_id))

    def get_banned(self) -> list:
        result = self._exec("SMEMBERS", "bot:banned")
        return result if result else []

    def is_banned(self, user_id: int) -> bool:
        result = self._exec("SISMEMBER", "bot:banned", str(user_id))
        return bool(result)

    def get_welcome(self) -> str:
        result = self._exec("GET", "bot:welcome")
        return result if result else ""

    def set_welcome(self, text: str):
        self._exec("SET", "bot:welcome", text)

    def reset_welcome(self):
        self._exec("DEL", "bot:welcome")

    def incr_search(self):
        self._exec("HINCRBY", "bot:stats", "total_searches", 1)

    def incr_play(self):
        self._exec("HINCRBY", "bot:stats", "total_plays", 1)

    def get_stats(self) -> dict:
        result = self._exec("HGETALL", "bot:stats")
        if result and isinstance(result, list):
            return {result[i]: int(result[i + 1]) for i in range(0, len(result), 2)}
        return {"total_searches": 0, "total_plays": 0}

    def get_cookie(self) -> str:
        result = self._exec("GET", "bot:cookie")
        return result if result else ""

    def set_cookie(self, cookie: str):
        self._exec("SET", "bot:cookie", cookie)
        self._exec("SET", "bot:cookie_updated_at", str(int(time.time())))

    def get_cookie_updated_at(self) -> int:
        result = self._exec("GET", "bot:cookie_updated_at")
        return int(result) if result else 0

    def get_quality(self) -> str:
        result = self._exec("GET", "bot:quality")
        return result if result else "standard"

    def set_quality(self, quality: str):
        self._exec("SET", "bot:quality", quality)

    def get_last_cookie_check(self) -> int:
        result = self._exec("GET", "bot:last_cookie_check")
        return int(result) if result else 0

    def set_last_cookie_check(self, ts: int):
        self._exec("SET", "bot:last_cookie_check", str(ts))

    def save_active_playlist(self, user_id: int, playlist_id: int, songs: list, current_index: int = 0):
        data = {
            "playlist_id": playlist_id, "current_index": current_index,
            "total": len(songs), "songs": songs, "start_time": int(time.time())
        }
        self._exec("SET", f"playlist:active:{user_id}", json.dumps(data, ensure_ascii=False), "EX", 86400)
        self._exec("SADD", "playlist:active_users", str(user_id))

    def get_active_playlist(self, user_id: int) -> dict:
        result = self._exec("GET", f"playlist:active:{user_id}")
        if not result:
            return {}
        try:
            return json.loads(result)
        except Exception:
            return {}

    def update_playlist_index(self, user_id: int, current_index: int):
        data = self.get_active_playlist(user_id)
        if not data:
            return
        data["current_index"] = current_index
        self._exec("SET", f"playlist:active:{user_id}", json.dumps(data, ensure_ascii=False), "EX", 86400)

    def remove_active_playlist(self, user_id: int):
        self._exec("DEL", f"playlist:active:{user_id}")
        self._exec("SREM", "playlist:active_users", str(user_id))

    def get_active_playlist_users(self) -> list:
        result = self._exec("SMEMBERS", "playlist:active_users")
        return [int(x) for x in result] if result else []

    def set_playlist_stop_flag(self, user_id: int):
        self._exec("SET", f"playlist:stop:{user_id}", "1", "EX", 60)

    def check_playlist_stop_flag(self, user_id: int) -> bool:
        exists = self._exec("EXISTS", f"playlist:stop:{user_id}")
        if exists:
            self._exec("DEL", f"playlist:stop:{user_id}")
            return True
        return False

    def get_file_id(self, song_id: int) -> str:
        result = self._exec("GET", f"cache:file_id:{song_id}")
        return result if result else ""

    def get_file_ids_batch(self, song_ids: list) -> dict:
        if not song_ids:
            return {}
        keys = [f"cache:file_id:{sid}" for sid in song_ids]
        results = self._exec("MGET", *keys)
        if not results:
            return {sid: "" for sid in song_ids}
        return {sid: (results[i] if i < len(results) and results[i] else "") for i, sid in enumerate(song_ids)}

    def set_file_id(self, song_id: int, file_id: str):
        self._exec("SET", f"cache:file_id:{song_id}", file_id)

    def add_searched_song(self, song_id: int):
        self._exec("SADD", "cache:searched_songs", str(song_id))

    def get_uncached_searched_songs(self, limit: int = 100) -> list:
        all_searched = self._exec("SMEMBERS", "cache:searched_songs") or []
        uncached = []
        for sid_str in all_searched:
            try:
                sid = int(sid_str)
                if not self.get_file_id(sid):
                    uncached.append(sid)
                    if len(uncached) >= limit:
                        break
            except (ValueError, TypeError):
                continue
        return uncached

    def clear_all_file_ids(self) -> int:
        keys = self._exec("KEYS", "cache:file_id:*")
        if not keys:
            return 0
        count = 0
        for k in keys:
            self._exec("DEL", k)
            count += 1
        return count

    def get_admins(self) -> list:
        result = self._exec("SMEMBERS", "bot:admins")
        return [int(x) for x in result] if result else []

    def add_admin(self, user_id: int):
        self._exec("SADD", "bot:admins", str(user_id))

    def remove_admin(self, user_id: int):
        self._exec("SREM", "bot:admins", str(user_id))

    def is_admin(self, user_id: int) -> bool:
        if user_id == config.ADMIN_ID:
            return True
        result = self._exec("SISMEMBER", "bot:admins", str(user_id))
        return bool(result)


# ============================================================
# 全局实例：根据配置选择数据库类型
# ============================================================
_db_type = getattr(config, "DB_TYPE", "sqlite").lower()

if _db_type == "upstash":
    db = UpstashDB()
else:
    db = SQLiteDB()

print(f"[Database] 使用数据库类型: {_db_type}")