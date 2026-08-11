"""
radar_routing.py — маршрутизация: анкета клиента → план поиска.

Клиент на регистрации указывает должность, грейд и локации. Дальше система
сама подбирает источники из базы (sources.json), без того чтобы клиент
вписывал Telegram-каналы руками.

Четыре принципа:
  1. Каналов и ключевых слов в коде нет — всё в базе. Добавили канал в таблицу,
     пересобрали sources.json → радар подхватил, деплой не нужен.
  2. Узкие сферы выше широких. Питонисту сначала питон-каналы, потом общие IT,
     потом «общее». Специфичность считается по числу источников в сфере.
  3. Непарсящееся физически не попадает в парсер (приватные инвайты, LinkedIn,
     названия без ссылок) — уходит в manual_links.
  4. Неизвестная должность не роняет поиск: не совпали ключевики → широкие
     каналы + hh по тексту как есть, причина пишется в skipped.

Использование:
    import radar_routing as rr
    base = rr.load_sources()
    plan = rr.build_plan(base, rr.ClientProfile(
        role="Senior Product Manager",
        level="senior, lead",
        locations="Москва, Удалённо",
        extra_channels=["myfavchannel"],
    ))
    plan.telegram_handles   # ['forchiefs', 'toplevel_job', ...]
    plan.search             # [{'name': 'hh.ru (API)', 'url': ...}, ...]
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

SOURCES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json")

# Значения поля «Уровень» из формы → метки уровней в базе источников.
# В форме поле свободное, подсказка предлагает junior/middle/senior/lead/head.
LEVEL_ALIASES: dict[str, str] = {
    "junior": "junior",
    "джун": "junior",
    "стажер": "junior",
    "стажёр": "junior",
    "intern": "junior",
    "middle": "middle",
    "мидл": "middle",
    "senior": "senior",
    "сеньор": "senior",
    "синьор": "senior",
    "lead": "top",
    "лид": "top",
    "head": "top",
    "хед": "top",
    "топ": "top",
    "director": "top",
    "директор": "top",
    "c-level": "top",
    "cto": "top",
    "ceo": "top",
    "фриланс": "freelance",
    "freelance": "freelance",
}

# Метки уровня в базе → какие клиентские уровни им подходят.
SOURCE_LEVEL_FIT: dict[str, set[str]] = {
    "junior / массовый": {"junior", "middle"},
    "массовый + middle": {"junior", "middle"},
    "middle–senior": {"middle", "senior", "top"},
    "middle–senior, топ": {"middle", "senior", "top"},
    "c-level / топ": {"top"},
    "фриланс": {"freelance"},
}

# Регионы hh.ru
HH_AREA: dict[str, str] = {
    "москва": "1",
    "санкт-петербург": "2",
    "спб": "2",
    "питер": "2",
    "россия": "113",
    "рф": "113",
}
HH_AREA_DEFAULT = "113"

REMOTE_MARKERS = ("удал", "remote", "из дома", "дистанц")


@dataclass
class ClientProfile:
    """Профиль клиента — собирается из полей анкеты."""
    role: str                              # «должность», поле target_titles
    level: str = ""                        # «грейд», поле seniority
    locations: str = ""                    # поле locations_allowed
    extra_channels: list[str] = field(default_factory=list)  # необязательное поле


@dataclass
class SearchPlan:
    telegram_handles: list[str] = field(default_factory=list)
    search: list[dict[str, Any]] = field(default_factory=list)
    manual_links: list[dict[str, Any]] = field(default_factory=list)
    spheres: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    remote: bool = False
    hh_area: str = HH_AREA_DEFAULT


def load_sources(path: str = SOURCES_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Разбор анкеты
# ---------------------------------------------------------------------------

def normalize_level(raw: str) -> set[str]:
    """'Senior, lead' → {'senior', 'top'}. Пусто → все уровни (не фильтруем)."""
    found: set[str] = set()
    low = (raw or "").lower()
    for alias, canonical in LEVEL_ALIASES.items():
        if alias in low:
            found.add(canonical)
    return found


def detect_remote(locations: str) -> bool:
    low = (locations or "").lower()
    return any(m in low for m in REMOTE_MARKERS)


def detect_hh_area(locations: str) -> str:
    """Первый распознанный город. Если только «удалённо» — вся Россия."""
    low = (locations or "").lower()
    for name, area in HH_AREA.items():
        if name in low:
            return area
    return HH_AREA_DEFAULT


# ---------------------------------------------------------------------------
# Матчинг сфер
# ---------------------------------------------------------------------------

CYRILLIC_RE = re.compile(r"[а-яё]")


def _keyword_hit(kw: str, text: str) -> bool:
    """Совпадение ключевого слова с учётом русских падежей.

    «маркетинг» должно ловить «маркетингу», «маркетингом» — поэтому для
    кириллицы разрешаем до 3 букв окончания. Для латиницы границы строгие,
    чтобы «ml» не находилось внутри «html».
    """
    kw = kw.strip()
    if not kw:
        return False
    esc = re.escape(kw)
    if CYRILLIC_RE.search(kw):
        pattern = rf"(?<![а-яёa-z0-9]){esc}[а-яё]{{0,3}}(?![а-яёa-z0-9])"
    else:
        pattern = rf"(?<![а-яёa-z0-9]){esc}(?![а-яёa-z0-9])"
    return bool(re.search(pattern, text))


def match_spheres(role: str, base: dict[str, Any]) -> list[str]:
    """Текст должности → список сфер, узкие первыми.

    Специфичность = число источников в сфере: чем меньше, тем уже сфера,
    тем выше её приоритет. Питон (4 канала) идёт раньше IT (30).
    """
    role_low = (role or "").lower()
    if not role_low:
        return []

    counts: dict[str, int] = {}
    for t in base["telegram"]:
        counts[t["sphere"]] = counts.get(t["sphere"], 0) + 1

    matched: list[tuple[int, str]] = []
    for sphere in base["spheres"]:
        name = sphere["name"]
        for kw in sphere["keywords"]:
            if _keyword_hit(kw, role_low):
                matched.append((counts.get(name, 0), name))
                break

    matched.sort(key=lambda x: x[0])          # узкие (мало источников) — первыми
    return [name for _, name in matched]


# ---------------------------------------------------------------------------
# Сборка плана
# ---------------------------------------------------------------------------

def level_fits(source_level: str, client_levels: set[str]) -> bool:
    """Подходит ли источник под грейд клиента."""
    if not client_levels:
        return True                            # грейд не указан — не фильтруем
    if not source_level:
        return True                            # у источника уровень не размечен — универсальный
    fit = SOURCE_LEVEL_FIT.get(source_level.strip().lower())
    if fit is None:
        return True                            # незнакомая метка — не режем
    return bool(fit & client_levels)


def build_plan(
    base: dict[str, Any],
    profile: ClientProfile,
    max_channels: int = 200,
) -> SearchPlan:
    plan = SearchPlan()
    plan.remote = detect_remote(profile.locations)
    plan.hh_area = detect_hh_area(profile.locations)

    client_levels = normalize_level(profile.level)
    spheres = match_spheres(profile.role, base)

    if not spheres:
        plan.skipped.append((profile.role or "(пусто)",
                             "должность не распознана — беру широкие каналы и hh по тексту"))

    # «общее» добавляем всегда последним, «удаленка» — если человек согласен на удалёнку
    ordered = list(spheres)
    if plan.remote:
        for extra in ("удаленка", "релокация и удаленка"):
            if extra not in ordered:
                ordered.append(extra)
    if "общее" not in ordered:
        ordered.append("общее")
    plan.spheres = ordered

    # --- Telegram ---
    seen: set[str] = set()

    # каналы клиента идут первыми и не фильтруются
    for h in profile.extra_channels:
        handle = h.strip().lstrip("@")
        if handle and handle.lower() not in seen:
            seen.add(handle.lower())
            plan.telegram_handles.append(handle)

    by_sphere: dict[str, list[dict[str, Any]]] = {}
    for t in base["telegram"]:
        by_sphere.setdefault(t["sphere"], []).append(t)

    for sphere in ordered:
        for t in by_sphere.get(sphere, []):
            if len(plan.telegram_handles) >= max_channels:
                break
            key = t["handle"].lower()
            if key in seen:
                continue
            if not level_fits(t.get("level", ""), client_levels):
                plan.skipped.append((t["handle"], f"грейд источника: {t.get('level')}"))
                continue
            seen.add(key)
            plan.telegram_handles.append(t["handle"])

    # --- Поисковые источники ---
    query = profile.role or ""
    experience = "moreThan6" if "top" in client_levels else \
                 "between3And6" if "senior" in client_levels else \
                 "between1And3" if "middle" in client_levels else \
                 "noExperience" if "junior" in client_levels else ""

    for s in base["search"]:
        url = (s["template"]
               .replace("{query}", query)
               .replace("{area}", plan.hh_area)
               .replace("{experience}", experience)
               .replace("{location}", profile.locations or ""))
        entry = {"name": s["name"], "url": url, "parseable": s["parseable"]}
        if s["parseable"]:
            plan.search.append(entry)
        else:
            plan.manual_links.append({"name": s["name"], "url": url,
                                      "reason": "автоматически не собирается"})

    # --- То, что отдаём человеку ссылками ---
    for m in base["manual"]:
        if m["sphere"] in ordered:
            plan.manual_links.append(m)

    return plan
