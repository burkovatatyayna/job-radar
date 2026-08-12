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

import digest as digest_core

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
        title = digest_core.item_title(r)
        company = digest_core.item_company(r)
        head = f"✅ {title}" + (f" — {company}" if company else "")
        lines.append(f"{head} (балл {r['score']['total']})\n{r.get('url')}")
    if len(fit) > 10:
