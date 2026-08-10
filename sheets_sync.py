"""
sheets_sync.py — синхронизация реестра с Google Таблицами (замена Notion).

Использует service account (не твой личный Google-логин) — так проще
автоматизировать через GitHub Actions без интерактивного входа.

Как получить доступ (см. README.md — там пошагово):
  1. Создать service account в Google Cloud Console, включить Google Sheets API.
  2. Скачать JSON-ключ service account'а.
  3. Открыть свою Google Таблицу → "Настройки доступа" → выдать доступ
     на редактирование e-mail'у service account'а (он выглядит как
     xxx@xxx.iam.gserviceaccount.com — есть в JSON-ключе).
  4. Положить содержимое JSON-ключа в секрет GitHub Actions GOOGLE_SERVICE_ACCOUNT_JSON.
  5. ID таблицы — это часть URL: docs.google.com/spreadsheets/d/<ЭТОТ_ID>/edit

Команды:
  python sheets_sync.py push   # выгрузить registry_rows.json в таблицу
  python sheets_sync.py pull   # скачать оценки пользователя в feedback.yaml
"""
from __future__ import annotations

import os
import sys
import json
from typing import Any

import gspread
import yaml
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_NAME = "Реестр"
HEADERS = [
    "Дата", "Заголовок", "Компания", "Ссылка", "Источник",
    "Балл", "Вердикт", "Причина (ИИ)", "Статус",
    "Моя оценка", "Почему",
]


def get_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError(
            "Не найден GOOGLE_SERVICE_ACCOUNT_JSON (переменная окружения / GitHub secret). "
            "См. инструкцию в шапке sheets_sync.py и в README.md."
        )
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet(sheet_id: str | None = None) -> gspread.Worksheet:
    """sheet_id можно передать явно (многопользовательский режим) — иначе
    берётся GOOGLE_SHEET_ID из окружения (однопользовательский режим)."""
    sheet_id = sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Не найден GOOGLE_SHEET_ID (переменная окружения / GitHub secret) и sheet_id не передан.")
    client = get_client()
    spreadsheet = client.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
    return ws


def push_rows(rows: list[dict[str, Any]], sheet_id: str | None = None) -> int:
    """Выгружает список вакансий (уже в памяти) в таблицу. Используется и
    однопользовательским push(), и multi_run.py напрямую. Возвращает
    количество реально добавленных (недублирующихся) строк."""
    ws = get_sheet(sheet_id)
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(HEADERS)
        existing_links = set()
    else:
        link_col = HEADERS.index("Ссылка")
        existing_links = {r[link_col] for r in existing[1:] if len(r) > link_col}

    import datetime as dt
    today = dt.date.today().isoformat()

    new_rows = []
    for r in rows:
        url = r.get("url", "")
        if url in existing_links:
            continue  # уже в таблице, не дублируем
        new_rows.append([
            today,
            r.get("title") or r.get("text", "")[:80],
            r.get("company") or r.get("channel", ""),
            url,
            r.get("source", ""),
            r.get("score", {}).get("total", ""),
            r.get("verdict", ""),
            r.get("score", {}).get("reasoning", ""),
            "хочу откликнуться",  # статус по умолчанию — правится руками в таблице
            "",  # моя оценка — заполняется руками
            "",  # почему — заполняется руками
        ])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"[sheets] Добавлено новых строк: {len(new_rows)} (пропущено дублей: {len(rows) - len(new_rows)})")
    return len(new_rows)


def pull_feedback(sheet_id: str | None = None) -> list[dict[str, Any]]:
    """Возвращает размеченные пользователем примеры (список dict) — используется
    и однопользовательским pull(), и multi_run.py напрямую."""
    ws = get_sheet(sheet_id)
    records = ws.get_all_records()

    examples = []
    for r in records:
        my_score = r.get("Моя оценка")
        if not my_score:
            continue
        examples.append({
            "title": r.get("Заголовок", ""),
            "company": r.get("Компания", ""),
            "my_score": my_score,
            "why": r.get("Почему", ""),
        })
    return examples


# ---- CLI-обёртки для однопользовательского режима (файлы + переменные окружения) ----

def push(rows_path: str = "registry_rows.json") -> None:
    with open(rows_path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    push_rows(rows)


def pull(feedback_path: str = "feedback.yaml") -> None:
    examples = pull_feedback()
    with open(feedback_path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"examples": examples}, f, allow_unicode=True, sort_keys=False)
    print(f"[sheets] Выгружено размеченных примеров: {len(examples)} → {feedback_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("push", "pull"):
        print("Использование: python sheets_sync.py [push|pull]")
        sys.exit(1)
    if sys.argv[1] == "push":
        push()
    else:
        pull()
