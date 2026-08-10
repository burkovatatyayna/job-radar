"""
fetch.py — сбор постов из публичных Telegram-каналов через веб-превью
https://t.me/s/<handle>.

Не использует Bot API и не логинится в аккаунт — просто читает обычную HTML-
страницу, которую Telegram отдаёт всем, включая ботов и незалогиненных.
Отдаёт последние ~20 постов канала. Для суточного окна этого достаточно.

Вывод: список dict вида
  {
    "source": "telegram",
    "channel": "<handle>",
    "text": "...",
    "url": "https://t.me/<handle>/<id>",
    "published_at": "2026-08-06T12:34:00+00:00",
  }
"""
from __future__ import annotations

import time
import datetime as dt
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = "channels.yaml"


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_channel_html(handle: str, settings: dict[str, Any]) -> str | None:
    url = f"https://t.me/s/{handle}"
    headers = {"User-Agent": settings.get("user_agent", "Mozilla/5.0")}
    try:
        resp = requests.get(url, headers=headers, timeout=settings.get("timeout_sec", 15))
        if resp.status_code != 200:
            print(f"[fetch] {handle}: HTTP {resp.status_code}, пропускаю")
            return None
        return resp.text
    except requests.RequestException as e:
        print(f"[fetch] {handle}: ошибка запроса — {e}")
        return None


def parse_posts(html: str, handle: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for wrap in soup.select("div.tgme_widget_message_wrap"):
        msg = wrap.select_one("div.tgme_widget_message")
        if not msg:
            continue
        post_id = msg.get("data-post")  # формат "<handle>/<id>"
        text_el = msg.select_one("div.tgme_widget_message_text")
        time_el = msg.select_one("time")

        text = text_el.get_text("\n", strip=True) if text_el else ""
        published_at = time_el.get("datetime") if time_el else None

        if not text:
            continue

        posts.append({
            "source": "telegram",
            "channel": handle,
            "text": text,
            "url": f"https://t.me/{post_id}" if post_id else f"https://t.me/s/{handle}",
            "published_at": published_at,
        })
    return posts


def within_window(post: dict[str, Any], window_hours: int) -> bool:
    if not post.get("published_at"):
        return True  # если не смогли распарсить дату — лучше показать, чем потерять
    try:
        published = dt.datetime.fromisoformat(post["published_at"])
    except ValueError:
        return True
    now = dt.datetime.now(published.tzinfo)
    return (now - published) <= dt.timedelta(hours=window_hours)


def fetch_all(config_path: str = CONFIG_PATH) -> list[dict[str, Any]]:
    config = load_config(config_path)
    settings = config.get("settings", {})
    window_hours = settings.get("window_hours", 24)
    delay = settings.get("request_delay_sec", 2)

    all_posts: list[dict[str, Any]] = []
    for ch in config.get("channels", []):
        handle = ch["handle"]
        html = fetch_channel_html(handle, settings)
        if html is None:
            time.sleep(delay)
            continue
        posts = parse_posts(html, handle)
        fresh = [p for p in posts if within_window(p, window_hours)]
        for p in fresh:
            p["category"] = ch.get("category", "mixed")
            p["weight"] = ch.get("weight", 1)
        print(f"[fetch] {handle}: {len(posts)} постов всего, {len(fresh)} за окно")
        all_posts.extend(fresh)
        time.sleep(delay)

    return all_posts


if __name__ == "__main__":
    import json
    result = fetch_all()
    print(f"\nИтого собрано: {len(result)} постов")
    with open("out_telegram.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
