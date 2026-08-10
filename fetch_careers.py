"""
fetch_careers.py — точечные парсеры карьерных сайтов компаний из careers.yaml.

Поддерживает два типа источников:
  - "html"     — обычная HTML-страница, парсим по CSS-селекторам
  - "json_api" — у компании есть свой JSON API, забираем напрямую

Каждая компания верстает страницу по-своему, поэтому конфиг в careers.yaml
описывает, ЧТО искать (селекторы / json_path), а код здесь — общий движок.
Если у компании совсем нестандартная страница — под неё можно дописать
отдельную функцию parse_<company>() и подключить в PARSERS.

Вывод: список dict вида
  {
    "source": "careers",
    "company": "...",
    "title": "...",
    "url": "...",
  }
"""
from __future__ import annotations

import time
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = "careers.yaml"


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_generic_html(company: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    headers = {"User-Agent": settings.get("user_agent", "Mozilla/5.0")}
    try:
        resp = requests.get(company["url"], headers=headers, timeout=settings.get("timeout_sec", 15))
    except requests.RequestException as e:
        print(f"[careers] {company['name']}: ошибка запроса — {e}")
        return []
    if resp.status_code != 200:
        print(f"[careers] {company['name']}: HTTP {resp.status_code}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    sel = company["selectors"]
    items = []
    for el in soup.select(sel["item"]):
        title_el = el.select_one(sel["title"]) if sel.get("title") else el
        title = title_el.get_text(strip=True) if title_el else None
        link = el.get(sel.get("link_attr", "href")) if el.name == "a" else None
        if not link and el.select_one("a"):
            link = el.select_one("a").get("href")
        if not title or not link:
            continue
        if link.startswith("/"):
            from urllib.parse import urljoin
            link = urljoin(company["url"], link)
        items.append({
            "source": "careers",
            "company": company["name"],
            "title": title,
            "url": link,
        })
    return items


def parse_json_api(company: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    headers = {"User-Agent": settings.get("user_agent", "Mozilla/5.0")}
    try:
        resp = requests.get(company["url"], headers=headers, timeout=settings.get("timeout_sec", 15))
    except requests.RequestException as e:
        print(f"[careers] {company['name']}: ошибка запроса — {e}")
        return []
    if resp.status_code != 200:
        print(f"[careers] {company['name']}: HTTP {resp.status_code}")
        return []

    data = resp.json()
    path = company["json_path"]

    def dig(obj: Any, dotted: str) -> Any:
        for part in dotted.split("."):
            if obj is None:
                return None
            obj = obj.get(part) if isinstance(obj, dict) else None
        return obj

    raw_items = dig(data, path["items"]) or []
    items = []
    for it in raw_items:
        title = dig(it, path["title"])
        link = dig(it, path["link"])
        if not title or not link:
            continue
        items.append({
            "source": "careers",
            "company": company["name"],
            "title": title,
            "url": link,
        })
    return items


PARSERS = {
    "html": parse_generic_html,
    "json_api": parse_json_api,
}


def fetch_all(config_path: str = CONFIG_PATH) -> list[dict[str, Any]]:
    config = load_config(config_path)
    settings = config.get("settings", {})
    delay = settings.get("request_delay_sec", 3)

    all_items: list[dict[str, Any]] = []
    for company in config.get("companies", []):
        parser = PARSERS.get(company.get("type"))
        if not parser:
            print(f"[careers] {company['name']}: неизвестный type={company.get('type')}, пропускаю")
            continue
        items = parser(company, settings)
        print(f"[careers] {company['name']}: {len(items)} позиций")
        all_items.extend(items)
        time.sleep(delay)

    return all_items


if __name__ == "__main__":
    import json
    result = fetch_all()
    print(f"\nИтого собрано: {len(result)} позиций")
    with open("out_careers.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
