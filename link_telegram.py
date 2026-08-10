"""
link_telegram.py — ловит нажатия "Start" в Telegram-боте по deep link'у из
формы регистрации (https://t.me/<bot>?start=<код>) и привязывает chat_id
к нужному пользователю в таблице Users.

Запускается часто (например, раз в 5-10 минут) через GitHub Actions —
это единственное "склеивающее" звено между формой и ботом.

Хранит смещение (offset) полученных апдейтов в Script-независимом месте:
файле offset.json в репозитории (коммитится тем же workflow-шагом, что и
остальное состояние).
"""
from __future__ import annotations

import os
import json

import requests

import users_sheet

OFFSET_PATH = "telegram_offset.json"
API_BASE = "https://api.telegram.org/bot{token}"


def _get_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не найдена переменная окружения {name}.")
    return val


def load_offset() -> int:
    if not os.path.exists(OFFSET_PATH):
        return 0
    with open(OFFSET_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("offset", 0)


def save_offset(offset: int) -> None:
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def get_updates(token: str, offset: int) -> list[dict]:
    url = API_BASE.format(token=token) + "/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_message(token: str, chat_id: int, text: str) -> None:
    url = API_BASE.format(token=token) + "/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text})


def main() -> None:
    token = _get_env("TELEGRAM_BOT_TOKEN")
    offset = load_offset()

    updates = get_updates(token, offset)
    if not updates:
        print("[link_telegram] новых апдейтов нет")
        return

    users = users_sheet.read_users()
    pending = {u["telegram_link_code"]: u for u in users if u.get("telegram_link_code") and not u.get("telegram_chat_id")}

    linked = 0
    max_update_id = offset - 1

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message") or {}
        text = msg.get("text", "")
        chat_id = (msg.get("chat") or {}).get("id")

        if not text.startswith("/start ") or chat_id is None:
            continue

        code = text.split(" ", 1)[1].strip()
        user = pending.get(code)
        if not user:
            continue

        users_sheet.update_cell(user["_row"], "telegram_chat_id", str(chat_id))
        send_message(token, chat_id,
                     "✅ Готово! Радар вакансий подключён — дайджест будет приходить сюда каждый день.")
        linked += 1
        print(f"[link_telegram] привязан пользователь {user.get('name')} → chat_id {chat_id}")

    save_offset(max_update_id + 1)
    print(f"[link_telegram] обработано апдейтов: {len(updates)}, привязано новых пользователей: {linked}")


if __name__ == "__main__":
    main()
