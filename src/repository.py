"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS replenish_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    fail_reason TEXT NOT NULL,
                    responsible_seller TEXT NOT NULL,
                    required_quantity INTEGER NOT NULL,
                    replenished_quantity INTEGER NOT NULL DEFAULT 0,
                    due_date TEXT NOT NULL,
                    penalty_amount REAL,
                    compensation_amount REAL,
                    created_by TEXT NOT NULL,
                    closed_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS replenish_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES replenish_tasks(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_record ON replenish_tasks(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_lots_task ON replenish_lots(task_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_single_open
                    ON replenish_tasks(record_id) WHERE status IN ('open', 'breached');
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    @staticmethod
    def _audit_insert(connection: sqlite3.Connection, record_id: int, action: str, actor_id: str, version: int, details: Dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def fail_with_task(
        self,
        record_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        actor_id: str,
        fail_reason: str,
        responsible_seller: str,
        shortfall_quantity: int,
        due_date: str,
        details: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """失败置位与补券任务创建在同一事务内完成。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='failed',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            try:
                cursor = connection.execute(
                    "INSERT INTO replenish_tasks(record_id,status,fail_reason,responsible_seller,required_quantity,replenished_quantity,due_date,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (record_id, "open", fail_reason, responsible_seller, shortfall_quantity, 0, due_date, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该指令已有未关闭的补券任务") from exc
            task_id = int(cursor.lastrowid)
            self._audit_insert(connection, record_id, "fail", actor_id, version, details, now)
            self._audit_insert(
                connection,
                record_id,
                "replenish_task_created",
                actor_id,
                version,
                {
                    "task_id": task_id,
                    "responsible_seller": responsible_seller,
                    "required_quantity": shortfall_quantity,
                    "due_date": due_date,
                },
                now,
            )
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            task = connection.execute("SELECT * FROM replenish_tasks WHERE id=?", (task_id,)).fetchone()
            connection.commit()
        return self._row(record), dict(task)

    def apply_replenishment(
        self,
        record_id: int,
        expected_version: int,
        quantity: int,
        note: str,
        actor_id: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """登记一笔补券；补够时关闭任务并把原指令恢复为待交收（approved）。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT state, version, payload FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            task_row = connection.execute(
                "SELECT * FROM replenish_tasks WHERE record_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if task_row is None:
                connection.rollback()
                raise Conflict("该指令没有进行中的补券任务")
            replenished = int(task_row["replenished_quantity"]) + quantity
            if replenished > int(task_row["required_quantity"]):
                connection.rollback()
                raise Conflict("补券数量超过待补缺口")
            completed = replenished >= int(task_row["required_quantity"])
            lot_cursor = connection.execute(
                "INSERT INTO replenish_lots(task_id,record_id,quantity,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (int(task_row["id"]), record_id, quantity, note, actor_id, now),
            )
            lot_id = int(lot_cursor.lastrowid)
            if completed:
                connection.execute(
                    "UPDATE replenish_tasks SET status='completed',replenished_quantity=?,closed_by=?,updated_at=?,closed_at=? WHERE id=?",
                    (replenished, actor_id, now, now, int(task_row["id"])),
                )
                version = int(expected_version) + 1
                connection.execute(
                    "UPDATE records SET state='approved',version=?,updated_by=?,updated_at=? WHERE id=?",
                    (version, actor_id, now, record_id),
                )
            else:
                connection.execute(
                    "UPDATE replenish_tasks SET replenished_quantity=?,updated_at=? WHERE id=?",
                    (replenished, now, int(task_row["id"])),
                )
                version = int(record_row["version"])
            lot = connection.execute("SELECT * FROM replenish_lots WHERE id=?", (lot_id,)).fetchone()
            task = connection.execute("SELECT * FROM replenish_tasks WHERE id=?", (int(task_row["id"]),)).fetchone()
            self._audit_insert(
                connection,
                record_id,
                "replenish",
                actor_id,
                version,
                {
                    "summary": "补券登记" if not completed else "补券登记，缺口补够",
                    "quantity": quantity,
                    "note": note,
                    "task_id": int(task_row["id"]),
                    "lot_id": lot_id,
                    "replenished_quantity": int(task["replenished_quantity"]),
                    "outstanding_quantity": max(0, int(task["required_quantity"]) - int(task["replenished_quantity"])),
                    "to": "approved" if completed else "failed",
                },
                now,
            )
            if completed:
                self._audit_insert(
                    connection,
                    record_id,
                    "replenish_completed",
                    actor_id,
                    version,
                    {"task_id": int(task_row["id"]), "required_quantity": int(task_row["required_quantity"])},
                    now,
                )
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(record), dict(task), dict(lot)

    def escalate_compensation(
        self,
        record_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        penalty_amount: float,
        outstanding_quantity: int,
        actor_id: str,
        details: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """截止日未补足：按未补市值记违约金，任务转违约，指令停在待赔付。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            task_row = connection.execute(
                "SELECT * FROM replenish_tasks WHERE record_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if task_row is None:
                connection.rollback()
                raise Conflict("该指令没有进行中的补券任务")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='compensation_pending',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE replenish_tasks SET status='breached',penalty_amount=?,updated_at=? WHERE id=?",
                (penalty_amount, now, int(task_row["id"])),
            )
            task = connection.execute("SELECT * FROM replenish_tasks WHERE id=?", (int(task_row["id"]),)).fetchone()
            self._audit_insert(connection, record_id, "escalate_compensation", actor_id, version, details, now)
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(record), dict(task)

    def confirm_compensation(
        self,
        record_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        compensation_amount: float,
        actor_id: str,
        details: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """确认赔付后任务关闭，指令方可冲正。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if str(record_row["state"]) != "compensation_pending":
                connection.rollback()
                raise Conflict("只有待赔付指令可以确认赔付")
            task_row = connection.execute(
                "SELECT * FROM replenish_tasks WHERE record_id=? AND status='breached' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if task_row is None:
                connection.rollback()
                raise Conflict("该指令没有待赔付的补券任务")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE replenish_tasks SET status='compensated',compensation_amount=?,closed_by=?,updated_at=?,closed_at=? WHERE id=?",
                (compensation_amount, actor_id, now, now, int(task_row["id"])),
            )
            task = connection.execute("SELECT * FROM replenish_tasks WHERE id=?", (int(task_row["id"]),)).fetchone()
            self._audit_insert(connection, record_id, "confirm_compensation", actor_id, version, details, now)
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(record), dict(task)

    def get_open_task(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM replenish_tasks WHERE record_id=? AND status IN ('open','breached') ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_lots(self, task_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM replenish_lots WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def replenishment_overview(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM replenish_tasks WHERE record_id=? ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_tasks_by_records(self, record_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM replenish_tasks WHERE id IN (SELECT MAX(id) FROM replenish_tasks WHERE record_id IN (%s) GROUP BY record_id)"
                % placeholders,
                record_ids,
            ).fetchall()
        return {int(row["record_id"]): dict(row) for row in rows}

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
