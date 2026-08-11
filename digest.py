"""
digest.py — ядро пайплайна.

СБОР (fetch.py / fetch_hh_scan.py / fetch_careers.py) → этот файл делает:
  ДЕДУП → КЛАССИФИКАЦИЯ → ОЦЕНКА (2 слоя) → ГЕНЕРАЦИЯ писем → HTML-дайджест → ПАМЯТЬ

Запуск: python digest.py
Результат: digest.html + registry_rows.json (для sheets_sync.py) + обновлённый state.json
"""
from __future__ import annotations

import os
import json
import hashlib
import datetime as dt
from typing import Any

import yaml

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # позволяет прогонять фильтры слоя 1 без установленного openai

PROFILE_PATH = "profile.yaml"
VOICE_PATH = "voice.md"
STATE_PATH = "state.json"
FEEDBACK_PATH = "feedback.yaml"

OPENAI_MODEL = "gpt-4o-mini"  # можно заменить на другую модель


# ---------------------------------------------------------------------------
# Загрузка входных данных
# ---------------------------------------------------------------------------

def load_yaml(path: str) -> Any:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: str) -> Any:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def collect_raw_items() -> list[dict[str, Any]]:
    """Подтягивает результаты всех трёх сборщиков, если файлы есть."""
    items: list[dict[str, Any]] = []
    for fname in ("out_telegram.json", "out_hh.json", "out_careers.json"):
        items.extend(load_json(fname))
    return items


# ---------------------------------------------------------------------------
# ДЕДУП
# ---------------------------------------------------------------------------

def normalize_text(s: str) -> str:
    return " ".join((s or "").lower().split())


def item_fingerprint(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("text", "")[:80]
    company = item.get("company") or item.get("channel", "")
    key = normalize_text(title) + "|" + normalize_text(company)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def dedup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for it in items:
        fp = item_fingerprint(it)
        it["fingerprint"] = fp
        if fp not in seen:
            seen[fp] = it
        else:
            # оставляем версию с более полным описанием
            if len(it.get("description", it.get("text", ""))) > \
               len(seen[fp].get("description", seen[fp].get("text", ""))):
                seen[fp] = it
    return list(seen.values())


# ---------------------------------------------------------------------------
# КЛАССИФИКАЦИЯ (эвристика, без ИИ — дешёвый первый проход)
# ---------------------------------------------------------------------------

NOISE_MARKERS = ["розыгрыш", "подборка каналов", "реклама", "промо"]
RESUME_MARKERS = ["ищу работу", "рассматриваю предложения", "резюме:"]


def classify(item: dict[str, Any], profile: dict[str, Any]) -> str:
    text = normalize_text(item.get("text", "") + " " + (item.get("title") or ""))
    noise_kw = [normalize_text(w) for w in profile.get("filters", {}).get("noise_keywords", [])]

    if any(m in text for m in NOISE_MARKERS + noise_kw):
        return "noise"
    if any(m in text for m in RESUME_MARKERS):
        return "resume_post"  # человек ищет работу — не вакансия, а конкурент :)
    if item.get("source") in ("hh.ru", "careers"):
        return "vacancy"
    # для Telegram-постов: похоже на вакансию, если есть характерные слова
    if any(w in text for w in ["вакансия", "ищем", "requirements", "обязанности", "зарплата"]):
        return "vacancy"
    return "market_news"


# ---------------------------------------------------------------------------
# ОЦЕНКА — Слой 1: детерминированные фильтры
# ---------------------------------------------------------------------------

def passes_layer1(item: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, str]:
    text = normalize_text(item.get("text", "") + " " + (item.get("title") or "") + " " +
                           (item.get("description", "")))
    filters = profile.get("filters", {})

    for bad in filters.get("anti_functions", []):
        if normalize_text(bad) in text:
            return False, f"анти-функция: {bad}"

    for bad in filters.get("anti_industries", []):
        if normalize_text(bad) in text:
            return False, f"анти-отрасль: {bad}"

    candidate = profile.get("candidate", {})
    allowed_locations = candidate.get("locations_allowed", [])

    # «Удалённо» — это формат работы, а не город: у удалённых вакансий на hh
    # всё равно проставлен город. Поэтому из списка локаций сначала выкидываем
    # маркеры удалёнки, а сам факт согласия на удалёнку запоминаем отдельно.
    remote_markers = ("удал", "remote", "из дома", "дистанц")
    city_locations = [loc for loc in allowed_locations
                      if not any(m in normalize_text(loc) for m in remote_markers)]
    remote_ok = candidate.get("remote_ok") or len(city_locations) < len(allowed_locations)

    # Если человек готов на удалёнку или городов не указано — по гео не режем вовсе.
    if city_locations and not remote_ok:
        area = normalize_text(item.get("area", ""))
        if area and not any(normalize_text(loc) in area or area in normalize_text(loc)
                             for loc in city_locations):
            if "удал" not in text and "remote" not in text:
                return False, f"гео не подходит: {item.get('area')}"

    for flag in profile.get("red_flags", []):
        if normalize_text(flag) in text:
            return False, f"красный флаг: {flag}"

    return True, ""


# ---------------------------------------------------------------------------
# ОЦЕНКА — Слой 2: ИИ по рубрике
# ---------------------------------------------------------------------------

def build_scoring_prompt(item: dict[str, Any], profile: dict[str, Any], feedback: list[dict]) -> str:
    rubric = profile.get("scoring_rubric", [])
    rubric_txt = "\n".join(f"- {r['key']} ({r['label']}), максимум {r['max_score']} баллов"
                            for r in rubric)

    few_shot = ""
    if feedback:
        examples = feedback[-10:]  # последние 10 размеченных примеров
        few_shot = "\n\nПримеры прошлых оценок пользователя (учитывай эти паттерны):\n"
        for ex in examples:
            few_shot += f"- «{ex.get('title', '')}»: оценка {ex.get('my_score')}/5, причина: {ex.get('why', '')}\n"

    return f"""Ты помогаешь оценить вакансию под профиль кандидата.

Профиль кандидата:
{profile.get('candidate', {}).get('summary', '')}
Желаемые роли: {', '.join(profile.get('candidate', {}).get('target_titles', []))}
Уровень: {', '.join(profile.get('candidate', {}).get('seniority', []))}

Вакансия:
Заголовок: {item.get('title') or item.get('text', '')[:200]}
Компания: {item.get('company') or item.get('channel', '')}
Описание: {(item.get('description') or item.get('text', ''))[:2000]}

Оцени по рубрике, верни ТОЛЬКО JSON без пояснений вокруг:
{{
{chr(10).join(f'  "{r["key"]}": <число 0-{r["max_score"]}>,' for r in rubric)}
  "reasoning": "<1-2 предложения почему>"
}}

Рубрика:
{rubric_txt}
{few_shot}
"""


def score_with_ai(item: dict[str, Any], profile: dict[str, Any], feedback: list[dict],
                   client: "OpenAI | None") -> dict[str, Any]:
    if client is None:
        return {"total": 0, "reasoning": "OPENAI_API_KEY не задан — оценка пропущена", "raw": {}}

    prompt = build_scoring_prompt(item, profile, feedback)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = json.loads(resp.choices[0].message.content)
    except Exception as e:
        return {"total": 0, "reasoning": f"ошибка ИИ-оценки: {e}", "raw": {}}

    rubric = profile.get("scoring_rubric", [])
    total = 0
    for r in rubric:
        val = raw.get(r["key"], 0)
        try:
            val = float(val)
        except (TypeError, ValueError):
            val = 0
        total += min(val, r["max_score"])

    return {"total": round(total, 1), "reasoning": raw.get("reasoning", ""), "raw": raw}


def verdict_from_score(total: float, profile: dict[str, Any]) -> str:
    th = profile.get("thresholds", {})
    if total >= th.get("fit", 70):
        return "подходит"
    if total >= th.get("borderline", 50):
        return "на грани"
    return "не подходит"


# ---------------------------------------------------------------------------
# ГЕНЕРАЦИЯ сопроводительного письма (только для "подходит")
# ---------------------------------------------------------------------------

def generate_cover_letter(item: dict[str, Any], profile: dict[str, Any],
                           voice: str, client: "OpenAI | None") -> str:
    if client is None:
        return ""
    prompt = f"""Напиши короткое сопроводительное письмо на русском для вакансии ниже,
строго следуя стилю из инструкции по тону.

Инструкция по тону:
{voice}

Профиль кандидата: {profile.get('candidate', {}).get('summary', '')}

Вакансия: {item.get('title') or item.get('text', '')[:200]}
Компания: {item.get('company') or item.get('channel', '')}
Описание: {(item.get('description') or item.get('text', ''))[:1500]}

Верни только текст письма, без заголовков и пояснений."""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[не удалось сгенерировать письмо: {e}]"


# ---------------------------------------------------------------------------
# ПАМЯТЬ — что уже показывали
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    return load_json(STATE_PATH) if os.path.exists(STATE_PATH) else {"shown_fingerprints": []}


def save_state(state: dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTML-дайджест
# ---------------------------------------------------------------------------

def render_html(rows: list[dict[str, Any]]) -> str:
    today = dt.date.today().isoformat()
    fit = [r for r in rows if r["verdict"] == "подходит"]
    border = [r for r in rows if r["verdict"] == "на грани"]

    def row_html(r: dict[str, Any]) -> str:
        letter = f"<details><summary>Сопроводительное письмо</summary><p>{r.get('cover_letter', '')}</p></details>" \
            if r.get("cover_letter") else ""
        return f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:10px;">
          <b><a href="{r.get('url','')}">{r.get('title','(без заголовка)')}</a></b><br>
          <span style="color:#666">{r.get('company','')} · {r.get('area','')} · балл {r['score']['total']}</span>
          <p style="color:#333">{r['score'].get('reasoning','')}</p>
          {letter}
        </div>"""

    html = f"""<html><head><meta charset="utf-8"></head><body style="font-family:sans-serif;max-width:700px;margin:auto;">
    <h2>📡 Радар вакансий — {today}</h2>
    <h3>✅ Подходит ({len(fit)})</h3>
    {''.join(row_html(r) for r in fit) or '<p>Ничего не найдено.</p>'}
    <h3>🤔 На грани ({len(border)})</h3>
    {''.join(row_html(r) for r in border) or '<p>Ничего.</p>'}
    </body></html>"""
    return html


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    profile = load_yaml(PROFILE_PATH)
    voice = ""
    if os.path.exists(VOICE_PATH):
        with open(VOICE_PATH, "r", encoding="utf-8") as f:
            voice = f.read()
    feedback = load_yaml(FEEDBACK_PATH).get("examples", []) if os.path.exists(FEEDBACK_PATH) else []
    state = load_state()
    shown = set(state.get("shown_fingerprints", []))

    api_key = os.environ.get("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key) if (OpenAI and api_key) else None
    if client is None:
        print("[digest] Внимание: OPENAI_API_KEY не найден — будет работать только слой 1 (без ИИ-оценки).")

    raw_items = collect_raw_items()
    print(f"[digest] Собрано сырых элементов: {len(raw_items)}")

    items = dedup(raw_items)
    print(f"[digest] После дедупа: {len(items)}")

    rows: list[dict[str, Any]] = []
    for item in items:
        if item["fingerprint"] in shown:
            continue  # уже показывали раньше

        item_class = classify(item, profile)
        if item_class != "vacancy":
            continue

        ok, reason = passes_layer1(item, profile)
        if not ok:
            continue

        score = score_with_ai(item, profile, feedback, client)
        verdict = verdict_from_score(score["total"], profile)
        if verdict == "не подходит":
            shown.add(item["fingerprint"])
            continue

        cover_letter = ""
        if verdict == "подходит":
            cover_letter = generate_cover_letter(item, profile, voice, client)

        rows.append({
            **item,
            "score": score,
            "verdict": verdict,
            "cover_letter": cover_letter,
        })
        shown.add(item["fingerprint"])

    rows.sort(key=lambda r: r["score"]["total"], reverse=True)

    html = render_html(rows)
    with open("digest.html", "w", encoding="utf-8") as f:
        f.write(html)

    with open("registry_rows.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    state["shown_fingerprints"] = list(shown)
    save_state(state)

    print(f"[digest] Готово: {len(rows)} вакансий в дайджесте "
          f"({sum(1 for r in rows if r['verdict']=='подходит')} подходит, "
          f"{sum(1 for r in rows if r['verdict']=='на грани')} на грани)")


if __name__ == "__main__":
    main()
