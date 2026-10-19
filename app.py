"""Обработчик входящих заявок с защитой от повторной доставки."""

import sqlite3
from contextlib import closing
from typing import Any


_REQUIRED_FIELDS = ("source", "external_id", "customer", "message")


def _prepare_db(connection: sqlite3.Connection) -> None:
    """Создаёт используемые таблицы и ограничение уникальности заявки."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            customer TEXT NOT NULL,
            message TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_source_external_id
            ON leads (source, external_id);
        """
    )


def _validate_event(event: Any) -> tuple[dict[str, str] | None, str | None]:
    """Проверяет событие и возвращает нормализованные строки либо причину отказа."""
    if not isinstance(event, dict):
        return None, "Событие должно быть словарём."

    normalized: dict[str, str] = {}
    for field in _REQUIRED_FIELDS:
        value = event.get(field)
        if not isinstance(value, str):
            return None, f"Поле '{field}' должно быть строкой."
        value = value.strip()
        if not value:
            return None, f"Поле '{field}' не должно быть пустым."
        normalized[field] = value

    return normalized, None


def process_event(event: dict, db_path: str) -> dict:
    """Создаёт заявку и уведомление или возвращает результат обработки повтора."""
    normalized, reason = _validate_event(event)
    if reason is not None:
        return {"status": "invalid", "reason": reason}

    # closing важен отдельно от транзакционного управления: sqlite3.Connection
    # в конструкции with завершает транзакцию, но не закрывает файловый дескриптор.
    with closing(sqlite3.connect(db_path)) as connection:
        _prepare_db(connection)
        # Сериализуем проверку и вставку, чтобы два параллельных повтора не
        # прошли проверку одновременно и не создали две заявки.
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT id FROM leads WHERE source = ? AND external_id = ?",
                (normalized["source"], normalized["external_id"]),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return {"status": "duplicate", "id": int(existing[0])}

            cursor = connection.execute(
                """
                INSERT INTO leads (source, external_id, customer, message)
                VALUES (?, ?, ?, ?)
                """,
                tuple(normalized[field] for field in _REQUIRED_FIELDS),
            )
            lead_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO notifications (lead_id) VALUES (?)", (lead_id,)
            )
            connection.commit()
            return {"status": "created", "id": lead_id}
        except Exception:
            connection.rollback()
            raise
