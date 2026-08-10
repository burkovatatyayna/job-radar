"""
notify.py — отправка дайджеста в Telegram через твоего собственного бота
(создаётся один раз через @BotFather, см. README.md).

Telegram Bot API не умеет красиво рендерить HTML-файл целиком в чат,
поэтому: 1) шлём короткую текстовую сводку сообщением, 2) прикладываем
digest.html файлом — можно открыть в браузере на телефоне.
"""
from __future__ import annotations

import os
import json
import requests

API_BASE = "https://api.telegram.org/bot{token}"


def _get_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не найдена переменная окружения {name} (GitHub secret).")
    return val


def build_summary_text_from_rows(rows: list) -> str:
    fit = [r for r in rows if r["verdict"] == "подходит"]
    border = [r for r in rows if r["verdict"] == "на грани"]

    if not fit and not border:
        return "📡 Радар: сегодня ничего подходящего не найдено."

    lines = [f"📡 Радар вакансий: {len(fit)} подходит, {len(border)} на грани\n"]
    for r in fit[:10]:
        lines.append(f"✅ {r.get('title')} — {r.get('company')} (балл {r['score']['total']})\n{r.get('url')}")
    if len(fit) > 10:
        lines.append(f"…и ещё {len(fit) - 10} в полном дайджесте (файл ниже).")

    return "\n\n".join(lines)


def build_summary_text(rows_path: str = "registry_rows.json") -> str:
    if not os.path.exists(rows_path):
        return "Сегодня новых вакансий не найдено."
    with open(rows_path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    return build_summary_text_from_rows(rows)


def send_message(text: str, token: str | None = None, chat_id: str | None = None) -> None:
    token = token or _get_env("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or _get_env("TELEGRAM_CHAT_ID")
    url = API_BASE.format(token=token) + "/sendMessage"
    # Telegram режет сообщения по 4096 символов — на всякий случай режем сами
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        resp = requests.post(url, data={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})
        if resp.status_code != 200:
            print(f"[notify] ошибка отправки сообщения: {resp.status_code} {resp.text}")


def send_document(path: str, token: str | None = None, chat_id: str | None = None) -> None:
    if not os.path.exists(path):
        return
    token = token or _get_env("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or _get_env("TELEGRAM_CHAT_ID")
    url = API_BASE.format(token=token) + "/sendDocument"
    with open(path, "rb") as f:
        resp = requests.post(url, data={"chat_id": chat_id}, files={"document": f})
    if resp.status_code != 200:
        print(f"[notify] ошибка отправки файла: {resp.status_code} {resp.text}")


def notify_user(token: str, chat_id: str, rows: list, html_path: str | None = None) -> None:
    """Используется multi_run.py — шлёт дайджест конкретному пользователю."""
    text = build_summary_text_from_rows(rows)
    send_message(text, token=token, chat_id=chat_id)
    if html_path:
        send_document(html_path, token=token, chat_id=chat_id)


def main() -> None:
    text = build_summary_text()
    send_message(text)
    send_document("digest.html")
    print("[notify] Дайджест отправлен в Telegram.")


if __name__ == "__main__":
    main()
