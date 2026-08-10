"""
fetch_hh_scan.py — сканер hh.ru.

Два прохода:
  1) по целевым компаниям (employer_id из hh.yaml)
  2) по рынку — по заголовкам вакансий (title_queries из hh.yaml)

Используем открытый JSON API hh.ru (https://api.hh.ru/vacancies) — он не требует
OAuth для чтения публичных вакансий. Если в какой-то момент он начнёт резать
запросы — здесь же есть fallback-парсер HTML страниц поиска (см. fallback_html_search),
но по умолчанию используем API: он стабильнее и официально предназначен для чтения.

Каждую найденную вакансию дополнительно обогащаем полным описанием через
карточку вакансии (JSON-LD, schema.org/JobPosting), если enrich_with_description=true.

Вывод: список dict вида
  {
    "source": "hh.ru",
    "title": "...",
    "company": "...",
    "url": "https://hh.ru/vacancy/12345",
    "description": "...",
    "published_at": "2026-08-06T10:00:00+0300",
    "salary": {...} | None,
    "area": "Москва",
  }
"""
from __future__ import annotations

import time
import json
from typing import Any

import requests
import yaml

CONFIG_PATH = "hh.yaml"
API_BASE = "https://api.hh.ru/vacancies"


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _headers(settings: dict[str, Any]) -> dict[str, str]:
    return {"User-Agent": settings.get("user_agent", "Mozilla/5.0")}


def search_vacancies(
    settings: dict[str, Any],
    text: str | None = None,
    employer_id: str | None = None,
    area_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    per_page = settings.get("per_page", 20)
    max_pages = settings.get("max_pages", 3)
    delay = settings.get("request_delay_sec", 3)

    for page in range(max_pages):
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if text:
            params["text"] = text
        if employer_id:
            params["employer_id"] = employer_id
        if area_ids:
            params["area"] = area_ids

        try:
            resp = requests.get(
                API_BASE, params=params, headers=_headers(settings),
                timeout=settings.get("timeout_sec", 15),
            )
        except requests.RequestException as e:
            print(f"[hh] ошибка запроса: {e}")
            break

        if resp.status_code != 200:
            print(f"[hh] HTTP {resp.status_code} на странице {page}, останавливаюсь")
            break

        data = resp.json()
        items = data.get("items", [])
        if not items:
            break

        for it in items:
            results.append({
                "source": "hh.ru",
                "title": it.get("name"),
                "company": (it.get("employer") or {}).get("name"),
                "url": it.get("alternate_url"),
                "vacancy_id": it.get("id"),
                "description": it.get("snippet", {}).get("responsibility") or "",
                "published_at": it.get("published_at"),
                "salary": it.get("salary"),
                "area": (it.get("area") or {}).get("name"),
            })

        if page >= data.get("pages", 1) - 1:
            break
        time.sleep(delay)

    return results


def enrich_with_description(vacancy: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Дотягивает полное описание вакансии через API карточки (JSON-LD-эквивалент)."""
    vid = vacancy.get("vacancy_id")
    if not vid:
        return vacancy
    try:
        resp = requests.get(
            f"{API_BASE}/{vid}", headers=_headers(settings),
            timeout=settings.get("timeout_sec", 15),
        )
        if resp.status_code == 200:
            data = resp.json()
            vacancy["description"] = data.get("description", vacancy.get("description", ""))
    except requests.RequestException as e:
        print(f"[hh] не удалось обогатить {vid}: {e}")
    return vacancy


def fetch_all(config_path: str = CONFIG_PATH) -> list[dict[str, Any]]:
    config = load_config(config_path)
    settings = config.get("settings", {})
    area_ids = config.get("area_ids")
    delay = settings.get("request_delay_sec", 3)
    enrich = settings.get("enrich_with_description", True)

    all_vacancies: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Проход 1: целевые компании
    for company in config.get("target_companies", []):
        vacancies = search_vacancies(settings, employer_id=company["employer_id"], area_ids=area_ids)
        print(f"[hh] {company['name']}: {len(vacancies)} вакансий")
        for v in vacancies:
            if v["vacancy_id"] in seen_ids:
                continue
            seen_ids.add(v["vacancy_id"])
            v["match_reason"] = f"целевая компания: {company['name']}"
            all_vacancies.append(v)
        time.sleep(delay)

    # Проход 2: по рынку (по заголовкам)
    for title in config.get("title_queries", []):
        vacancies = search_vacancies(settings, text=title, area_ids=area_ids)
        print(f"[hh] запрос «{title}»: {len(vacancies)} вакансий")
        for v in vacancies:
            if v["vacancy_id"] in seen_ids:
                continue
            seen_ids.add(v["vacancy_id"])
            v["match_reason"] = f"рыночный поиск: {title}"
            all_vacancies.append(v)
        time.sleep(delay)

    # Обогащение описанием
    if enrich:
        for v in all_vacancies:
            enrich_with_description(v, settings)
            time.sleep(delay)

    return all_vacancies


if __name__ == "__main__":
    result = fetch_all()
    print(f"\nИтого собрано: {len(result)} вакансий")
    with open("out_hh.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
