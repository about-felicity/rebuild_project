from __future__ import annotations

import sqlite3
from typing import Any
from pathlib import Path
import json

DB_PATH = Path(__file__).resolve().parent / "app.db"

# 公共产品图等素材使用此 project_id 存库；``list_project_assets(真实项目)`` 会自动并入这些行。
FRAMEOS_SHARED_PROJECT_ID = "__frameos_shared__"

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                UNIQUE (project_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_project_seq
                ON chat_messages (project_id, seq);
            CREATE TABLE IF NOT EXISTS project_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                library TEXT NOT NULL CHECK (library IN ('video', 'asset', 'storyboard')),
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                uri TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pa_project_library
                ON project_assets (project_id, library);
            """
        )

def _query_chat_rows(project_id: str) -> list[sqlite3.Row]:
    """按 seq、再按 id 排序，避免仅 seq 时同序无稳定次序；供读库与 API 共用。"""
    pid = project_id.strip()
    with _connect() as conn:
        return list(
            conn.execute(
                """
                SELECT id, seq, role, content FROM chat_messages
                WHERE project_id = ?
                ORDER BY seq ASC, id ASC
                """,
                (pid,),
            ).fetchall()
        )


def load_history_for_model(project_id: str) -> list[dict[str, Any]]:
    """按顺序取出，供 agent_loop 使用；每条 content 为 str。"""
    return [
        {"role": str(r["role"]), "content": str(r["content"])}
        for r in _query_chat_rows(project_id)
    ]


def list_chat_messages_for_api(project_id: str) -> list[dict[str, Any]]:
    """含 seq/id，供 GET messages 与前端稳定排序。"""
    out: list[dict[str, Any]] = []
    for r in _query_chat_rows(project_id):
        out.append(
            {
                "id": int(r["id"]),
                "seq": int(r["seq"]),
                "role": str(r["role"]),
                "content": str(r["content"]),
            }
        )
    return out


def list_chat_project_ids_recent_first() -> list[str]:
    """库中有过对话的 project_id，最近有消息在前（供前端侧边栏与 localStorage 对不齐时恢复）。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT project_id FROM chat_messages
            GROUP BY project_id
            ORDER BY MAX(id) DESC
            """
        ).fetchall()
    return [str(r["project_id"]).strip() for r in rows if r["project_id"]]


def append_chat_user_message(project_id: str, user_text: str) -> None:
    """在调用模型前写入用户原文，刷新页面时仍能看到「已发送、待回复」。"""
    pid = project_id.strip()
    ut = (user_text or "").strip()
    if not ut:
        return
    with _connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT COALESCE(MAX(seq), -1) AS m
                FROM chat_messages WHERE project_id = ?
                """,
                (pid,),
            ).fetchone()
            s = int(row["m"]) + 1
            conn.execute(
                """
                INSERT INTO chat_messages (project_id, seq, role, content)
                VALUES (?, ?, 'user', ?)
                """,
                (pid, s, ut),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def append_chat_assistant_message(project_id: str, assistant_text: str) -> None:
    """本轮助手回复（须在 ``append_chat_user_message`` 之后调用）。"""
    pid = project_id.strip()
    at = assistant_text if assistant_text is not None else ""
    with _connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT COALESCE(MAX(seq), -1) AS m
                FROM chat_messages WHERE project_id = ?
                """,
                (pid,),
            ).fetchone()
            s = int(row["m"]) + 1
            conn.execute(
                """
                INSERT INTO chat_messages (project_id, seq, role, content)
                VALUES (?, ?, 'assistant', ?)
                """,
                (pid, s, at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
def clear_session(project_id: str) -> None:
    pid = project_id.strip()
    with _connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE project_id = ?", (pid,))


def delete_project_data(project_id: str) -> None:
    """删除该项目在库内的对话记录与素材元数据（不删磁盘上的 media 文件）。"""
    pid = project_id.strip()
    if pid == FRAMEOS_SHARED_PROJECT_ID:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM project_assets WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM chat_messages WHERE project_id = ?", (pid,))


def delete_project_asset_owned(project_id: str, asset_id: int) -> str | None:
    """
    删除属于 ``project_id`` 的一条 ``project_assets`` 记录（不匹配则不解删，如公共库条目挂在 __frameos_shared__）。
    返回：删除成功时该行的 ``uri``（供调用方删磁盘文件）；未删则 ``None``。
    """
    pid = project_id.strip()
    aid = int(asset_id)
    if not pid:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT uri FROM project_assets WHERE id = ? AND project_id = ?",
            (aid, pid),
        ).fetchone()
        if row is None:
            return None
        uri = str(row["uri"] or "")
        conn.execute("DELETE FROM project_assets WHERE id = ? AND project_id = ?", (aid, pid))
        conn.commit()
        return uri


def insert_project_asset(
    project_id: str,
    library: str,
    kind: str,
    name: str,
    uri: str = "",
    meta: dict[str, Any] | None = None,
) -> int:
    pid = project_id.strip()
    if library not in ("video", "asset", "storyboard"):
        raise ValueError("library must be video | asset | storyboard")
    blob = json.dumps(meta or {}, ensure_ascii=False)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO project_assets (project_id, library, kind, name, uri, meta_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (pid, library, kind, name, uri or "", blob),
        )
        return int(cur.lastrowid)


def _select_project_asset_rows(
    conn: sqlite3.Connection, project_id: str, library: str | None
) -> list[sqlite3.Row]:
    if library:
        return list(
            conn.execute(
                """
                SELECT id, project_id, library, kind, name, uri, meta_json, created_at
                FROM project_assets WHERE project_id = ? AND library = ?
                ORDER BY id ASC
                """,
                (project_id, library),
            ).fetchall()
        )
    return list(
        conn.execute(
            """
            SELECT id, project_id, library, kind, name, uri, meta_json, created_at
            FROM project_assets WHERE project_id = ?
            ORDER BY id ASC
            """,
            (project_id,),
        ).fetchall()
    )


def _rows_to_asset_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "library": r["library"],
                "kind": r["kind"],
                "name": r["name"],
                "uri": r["uri"],
                "meta": json.loads(r["meta_json"] or "{}"),
                "created_at": r["created_at"],
            }
        )
    return out


def list_project_assets(
    project_id: str, library: str | None = None
) -> list[dict[str, Any]]:
    """
    列出项目素材。任意非共享项目会自动包含 ``FRAMEOS_SHARED_PROJECT_ID`` 下的公共素材（如仓库 ``产品图/`` 同步项），
    且公共条目排在前面。
    """
    pid = project_id.strip()
    with _connect() as conn:
        if pid == FRAMEOS_SHARED_PROJECT_ID:
            rows = _select_project_asset_rows(conn, pid, library)
        else:
            shared = _select_project_asset_rows(conn, FRAMEOS_SHARED_PROJECT_ID, library)
            own = _select_project_asset_rows(conn, pid, library)
            rows = list(shared) + list(own)
    return _rows_to_asset_dicts(rows)