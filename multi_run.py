"""
multi_run.py — многопользовательский запуск.

Раз в сутки (по расписанию GitHub Actions) проходит по всем активным
пользователям из листа Users (см. users_sheet.py), для каждого:
  1. собирает вакансии из ЕГО Telegram-каналов и hh.ru-компаний/запросов
  2. дедуп → классификация → скоринг → письма (переиспользует digest.py)
  3. пишет результат в ЕГО личную Google Таблицу
  4. шлёт дайджест в ЕГО личный Telegram-чат (общий бот, разные chat_id)

Требует переменные окружения:
  GOOGLE_SERVICE_ACCOUNT_JSON, ADMIN_SHEET_ID  — доступ к листу Users
  OPENAI_API_KEY                                — общий ключ для всех пользователей
  TELEGRAM_BOT_TOKEN                            — общий бот для всех пользователей
"""
from __future__ import annotations

import os
import time
import traceback
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

import users_sheet
import fetch as fetch_telegram
import fetch_hh_scan
import sheets_sync
import notify
import digest as digest_core

# Общая (одна на всех пользователей) рубрика и пороги — можно вынести в отдельный
# YAML, если захотите разную строгость оценки для разных людей. Для MVP — общие.
DEFAULT_SCORING_RUBRIC = [
    {"key": "mandate", "label": "Мандат / уровень ответственности", "max_score": 25},
    {"key": "seniority", "label": "Соответствие уровню", "max_score": 20},
    {"key": "industry", "label": "Отрасль / продукт", "max_score": 15},
    {"key": "scale", "label": "Масштаб компании/продукта", "max_score": 15},
    {"key": "geo", "label": "География / формат работы", "max_score": 15},
    {"key": "growth", "label": "Потенциал роста/обучения", "max_score": 10},
]
DEFAULT_THRESHOLDS = {"fit": 70, "borderline": 50}
DEFAULT_ANTI_FUNCTIONS: list[str] = []
DEFAULT_NOISE_KEYWORDS = ["ищу работу", "резюме", "реклама", "подборка каналов", "розыгрыш"]
DEFAULT_RED_FLAGS = ["серая зарплата", "испытательный срок без оформления"]

TELEGRAM_SETTINGS = {
    "window_hours": 24,
    "request_delay_sec": 2,
    "timeout_sec": 15,
    "user_agent": "Mozilla/5.0 (compatible; PersonalJobRadar/1.0; +read-only)",
}
HH_SETTINGS = {
    "per_page": 20,
    "max_pages": 2,
    "request_delay_sec": 3,
    "timeout_sec": 15,
    "user_agent": "Mozilla/5.0 (compatible; PersonalJobRadar/1.0; +read-only)",
    "enrich_with_description": True,
}


def build_profile(user: dict[str, Any]) -> dict[str, Any]:
    """Собирает profile-словарь в том же формате, что и profile.yaml
    (переиспользуем все функции digest.py без изменений)."""
    return {
        "candidate": {
            "summary": user.get("candidate_summary", ""),
            "target_titles": users_sheet.split_csv(user.get("target_titles", "")),
            "seniority": users_sheet.split_csv(user.get("seniority", "")),
            "locations_allowed": users_sheet.split_csv(user.get("locations_allowed", "")),
        },
        "filters": {
            "anti_functions": DEFAULT_ANTI_FUNCTIONS,
            "anti_industries": [],
            "noise_keywords": DEFAULT_NOISE_KEYWORDS,
        },
        "scoring_rubric": DEFAULT_SCORING_RUBRIC,
        "thresholds": DEFAULT_THRESHOLDS,
        "red_flags": DEFAULT_RED_FLAGS,
    }


def collect_items_for_user(user: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    # --- Telegram-каналы пользователя ---
    channels = users_sheet.split_csv(user.get("channels", ""))
    for handle in channels:
        html = fetch_telegram.fetch_channel_html(handle, TELEGRAM_SETTINGS)
        if html is None:
            time.sleep(TELEGRAM_SETTINGS["request_delay_sec"])
            continue
        posts = fetch_telegram.parse_posts(html, handle)
        fresh = [p for p in posts if fetch_telegram.within_window(p, TELEGRAM_SETTINGS["window_hours"])]
        items.extend(fresh)
        time.sleep(TELEGRAM_SETTINGS["request_delay_sec"])

    # --- hh.ru: целевые компании пользователя ---
    hh_companies = users_sheet.parse_hh_companies(user.get("hh_companies", ""))
    seen_ids: set[str] = set()
    for company in hh_companies:
        vacancies = fetch_hh_scan.search_vacancies(HH_SETTINGS, employer_id=company["employer_id"])
        for v in vacancies:
            if v["vacancy_id"] in seen_ids:
                continue
            seen_ids.add(v["vacancy_id"])
            items.append(v)
        time.sleep(HH_SETTINGS["request_delay_sec"])

    # --- hh.ru: рыночный поиск по заголовкам пользователя ---
    hh_titles = users_sheet.split_csv(user.get("hh_titles", ""))
    for title in hh_titles:
        vacancies = fetch_hh_scan.search_vacancies(HH_SETTINGS, text=title)
        for v in vacancies:
            if v["vacancy_id"] in seen_ids:
                continue
            seen_ids.add(v["vacancy_id"])
            items.append(v)
        time.sleep(HH_SETTINGS["request_delay_sec"])

    if HH_SETTINGS.get("enrich_with_description"):
        for it in items:
            if it.get("source") == "hh.ru":
                fetch_hh_scan.enrich_with_description(it, HH_SETTINGS)
                time.sleep(HH_SETTINGS["request_delay_sec"])

    return items


def process_user(user: dict[str, Any], client: "OpenAI | None", telegram_token: str) -> None:
    name = user.get("name", "?")
    print(f"\n=== Пользователь: {name} ===")

    if user.get("status") != "active":
        print(f"[{name}] статус не active, пропускаю")
        return
    if not user.get("telegram_chat_id"):
        print(f"[{name}] ещё не привязал Telegram (не нажал Start), пропускаю")
        return
    if not user.get("personal_sheet_id"):
        print(f"[{name}] нет личной таблицы, пропускаю")
        return

    profile = build_profile(user)
    state = users_sheet.load_state(user)
    shown = set(state.get("shown_fingerprints", []))

    feedback_examples = sheets_sync.pull_feedback(sheet_id=user["personal_sheet_id"])

    raw_items = collect_items_for_user(user)
    print(f"[{name}] собрано сырых элементов: {len(raw_items)}")

    items = digest_core.dedup(raw_items)

    rows: list[dict[str, Any]] = []
    for item in items:
        if item["fingerprint"] in shown:
            continue

        item_class = digest_core.classify(item, profile)
        if item_class != "vacancy":
            continue

        ok, _ = digest_core.passes_layer1(item, profile)
        if not ok:
            continue

        score = digest_core.score_with_ai(item, profile, feedback_examples, client)
        verdict = digest_core.verdict_from_score(score["total"], profile)
        if verdict == "не подходит":
            shown.add(item["fingerprint"])
            continue

        cover_letter = ""
        if verdict == "подходит":
            cover_letter = digest_core.generate_cover_letter(item, profile, "", client)

        rows.append({**item, "score": score, "verdict": verdict, "cover_letter": cover_letter})
        shown.add(item["fingerprint"])

    rows.sort(key=lambda r: r["score"]["total"], reverse=True)
    print(f"[{name}] в дайджесте: {len(rows)} "
          f"({sum(1 for r in rows if r['verdict']=='подходит')} подходит, "
          f"{sum(1 for r in rows if r['verdict']=='на грани')} на грани)")

    if rows:
        sheets_sync.push_rows(rows, sheet_id=user["personal_sheet_id"])
        html = digest_core.render_html(rows)
        html_path = f"digest_{user['user_id']}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        notify.notify_user(telegram_token, user["telegram_chat_id"], rows, html_path=html_path)
    else:
        notify.send_message("Сегодня новых подходящих вакансий не найдено.",
                             token=telegram_token, chat_id=user["telegram_chat_id"])

    state["shown_fingerprints"] = list(shown)
    users_sheet.save_state(user, state)


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key) if (OpenAI and api_key) else None
    if client is None:
        print("[multi_run] Внимание: OPENAI_API_KEY не найден — ИИ-оценка отключена для всех пользователей.")

    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]

    users = users_sheet.read_users()
    print(f"[multi_run] Всего пользователей в таблице: {len(users)}")

    for user in users:
        try:
            process_user(user, client, telegram_token)
        except Exception as e:
            # Ошибка у одного пользователя не должна ронять весь прогон остальных
            print(f"[multi_run] ОШИБКА у пользователя {user.get('name')}: "
                  f"{type(e).__name__}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
