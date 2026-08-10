"""
users_sheet.py — работа с листом "Users" в административной Google Таблице.
Общий модуль для multi_run.py и link_telegram.py.

Требует переменные окружения:
  GOOGLE_SERVICE_ACCOUNT_JSON — тот же service account, что и для sheets_sync.py
  ADMIN_SHEET_ID              — ID административной таблицы (см. signup/README.md)
"""
from __future__ import annotations

import os
import json
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
USERS_SHEET_NAME = "Users"

# Порядок должен совпадать с USERS_HEADERS в signup/Code.gs
COLUMNS = [
    "user_id", "name", "email", "status",
    "candidate_summary", "target_titles", "seniority", "locations_allowed",
    "channels", "hh_companies", "hh_titles",
    "telegram_link_code", "telegram_chat_id",
    "personal_sheet_id", "state_json", "created_at",
]


def get_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("Не найден GOOGLE_SERVICE_ACCOUNT_JSON.")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_users_worksheet() -> gspread.Worksheet:
    admin_sheet_id = os.environ.get("ADMIN_SHEET_ID")
    if not admin_sheet_id:
        raise RuntimeError("Не найден ADMIN_SHEET_ID.")
    client = get_client()
    spreadsheet = client.open_by_key(admin_sheet_id)
    return spreadsheet.worksheet(USERS_SHEET_NAME)


def read_users() -> list[dict[str, Any]]:
    """Возвращает всех пользователей списком dict. Индекс строки в таблице
    (1-based, с учётом заголовка) кладём в поле _row для последующих апдейтов."""
    ws = get_users_worksheet()
    records = ws.get_all_records()
    for i, r in enumerate(records):
        r["_row"] = i + 2  # +1 за заголовок, +1 потому что gspread 1-based
    return records


def update_cell(row: int, column_name: str, value: Any) -> None:
    ws = get_users_worksheet()
    col_idx = COLUMNS.index(column_name) + 1
    ws.update_cell(row, col_idx, value)


def split_csv(value: str) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_hh_companies(value: str) -> list[dict[str, str]]:
    """'Яндекс:123, Авито:456' -> [{'name': 'Яндекс', 'employer_id': '123'}, ...]"""
    result = []
    for pair in split_csv(value):
        if ":" in pair:
            name, eid = pair.split(":", 1)
            result.append({"name": name.strip(), "employer_id": eid.strip()})
    return result


def load_state(user: dict[str, Any]) -> dict[str, Any]:
    raw = user.get("state_json") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"shown_fingerprints": []}


def save_state(user: dict[str, Any], state: dict[str, Any], max_fingerprints: int = 2000) -> None:
    fps = state.get("shown_fingerprints", [])
    if len(fps) > max_fingerprints:
        fps = fps[-max_fingerprints:]  # не даём полю расти бесконечно
    state["shown_fingerprints"] = fps
    update_cell(user["_row"], "state_json", json.dumps(state, ensure_ascii=False))
