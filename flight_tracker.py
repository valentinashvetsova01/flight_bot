#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Трекер билетов из Италии в Азию.

Раз в запуск присылает в Telegram ТРИ подборки — топ-5 самых дешёвых
реальных билетов туда-обратно (поездка 12–16 дней) в заданном окне дат:
  1) Италия → Шри-Ланка
  2) Италия → Таиланд
  3) Италия → Китай

Присылает всегда (не важно, новые цены или нет) — это просто актуальный
срез «что дешевле всего прямо сейчас».

Запуск: python flight_tracker.py
Секреты и настройки — из переменных окружения (см. README).
"""

import os
import json
import time
import html
from datetime import datetime, date, timezone

import sys

import requests

# Windows-консоль по умолчанию не печатает кириллицу — принудительно UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _load_dotenv(path=".env"):
    """Простая подгрузка переменных из файла .env (без внешних зависимостей)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TP_MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "").strip()

CURRENCY = os.environ.get("CURRENCY", "eur")
CURRENCY_SIGN = {"eur": "€", "rub": "₽", "usd": "$"}.get(CURRENCY, CURRENCY.upper())

# Сколько билетов показывать в каждой подборке.
TOP_N = int(os.environ.get("TOP_N", "5"))

# Верхний потолок здравого смысла — чтобы в топ не лезла явная дичь.
MAX_PRICE = int(os.environ.get("MAX_PRICE", "3000"))

# Окно дат и длительность поездки.
DEPART_FROM = os.environ.get("DEPART_FROM", "2026-12-23")
RETURN_TO = os.environ.get("RETURN_TO", "2027-03-31")
MIN_TRIP_DAYS = int(os.environ.get("MIN_TRIP_DAYS", "12"))
MAX_TRIP_DAYS = int(os.environ.get("MAX_TRIP_DAYS", "16"))

DEPART_MONTHS = os.environ.get(
    "DEPART_MONTHS", "2026-12,2027-01,2027-02,2027-03"
).split(",")

LIMIT = int(os.environ.get("LIMIT", "100"))

# Аэропорты вылета в Италии (IATA). API часто нормализует их к кодам городов.
ITALY_AIRPORTS = os.environ.get(
    "ITALY_AIRPORTS",
    "FCO,MXP,BGY,VCE,BLQ,NAP,TRN,PSA,CTA,BRI,VRN,FLR"
).split(",")

# Направления, сгруппированные по странам. Порядок = порядок подборок.
COUNTRIES = [
    {"name": "Шри-Ланка", "flag": "🇱🇰", "airports": ["CMB"]},
    {"name": "Таиланд", "flag": "🇹🇭",
     "airports": ["BKK", "DMK", "HKT", "CNX", "USM", "KBV"]},
    {"name": "Китай", "flag": "🇨🇳",
     "airports": ["PEK", "PKX", "PVG", "SHA", "CAN", "CTU"]},
]

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

# Файл для записи последнего прогона (нужен, чтобы GitHub не отключал расписание
# из-за неактивности репозитория — workflow коммитит его обратно).
STATE_FILE = os.environ.get("STATE_FILE", "history.json")

MONTHS_RU = ["", "янв", "фев", "мар", "апр", "мая", "июн",
             "июл", "авг", "сен", "окт", "ноя", "дек"]
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# Города вылета (коды аэропортов и кодов городов, которые отдаёт API).
CITY_NAMES = {
    "FCO": "Рим", "MXP": "Милан", "BGY": "Бергамо", "VCE": "Венеция",
    "BLQ": "Болонья", "NAP": "Неаполь", "TRN": "Турин", "PSA": "Пиза",
    "CTA": "Катания", "BRI": "Бари", "VRN": "Верона", "FLR": "Флоренция",
    "ROM": "Рим", "MIL": "Милан",
}

# Аэропорты прилёта (и коды городов, которые API отдаёт вместо аэропортов).
AIRPORT_NAMES = {
    "CMB": "Коломбо",
    "BKK": "Бангкок", "DMK": "Бангкок (Дон Муанг)", "HKT": "Пхукет",
    "CNX": "Чиангмай", "USM": "Самуи", "KBV": "Краби",
    "PEK": "Пекин", "PKX": "Пекин (Дасин)", "BJS": "Пекин",
    "PVG": "Шанхай", "SHA": "Шанхай", "CAN": "Гуанчжоу", "CTU": "Чэнду",
}


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def fmt_price(p):
    return f"{p:,}".replace(",", " ") + f" {CURRENCY_SIGN}"


def fmt_date(iso):
    """'2027-01-11' -> '11 янв (сб)'."""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso[:10]
    return f"{d.day} {MONTHS_RU[d.month]} ({WEEKDAYS_RU[d.weekday()]})"


def trip_days(deal):
    try:
        return (date.fromisoformat(deal.get("return_at", "")[:10])
                - date.fromisoformat(deal.get("departure_at", "")[:10])).days
    except ValueError:
        return None


def in_window(deal):
    """Вылет и возврат в окне дат И длительность поездки в нужном диапазоне."""
    dep = deal.get("departure_at", "")[:10]
    ret = deal.get("return_at", "")[:10]
    if not dep or not ret:
        return False
    try:
        d_from = date.fromisoformat(DEPART_FROM)
        r_to = date.fromisoformat(RETURN_TO)
        dep_d = date.fromisoformat(dep)
        ret_d = date.fromisoformat(ret)
    except ValueError:
        return False
    days = (ret_d - dep_d).days
    return (dep_d >= d_from and ret_d <= r_to
            and MIN_TRIP_DAYS <= days <= MAX_TRIP_DAYS)


def month_after(m):
    y, mo = map(int, m.split("-"))
    if mo == 12:
        y, mo = y + 1, 1
    else:
        mo += 1
    return f"{y:04d}-{mo:02d}"


def build_link(deal):
    link = deal.get("link", "")
    if not link:
        return ""
    url = "https://www.aviasales.ru" + link
    if TP_MARKER:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}marker={TP_MARKER}"
    return url


# ---------------------------------------------------------------------------
# Запрос к API
# ---------------------------------------------------------------------------

def fetch_prices(origin, destination, depart_month, return_month):
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart_month,
        "return_at": return_month,
        "currency": CURRENCY,
        "one_way": "false",
        "sorting": "price",
        "limit": LIMIT,
        "page": 1,
        "market": "ru",
    }
    headers = {"X-Access-Token": TP_TOKEN}
    try:
        r = requests.get(API_URL, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  ! Ошибка {origin}->{destination} ({depart_month}/{return_month}): {e}")
        return []
    if not payload.get("success", False):
        return []
    return payload.get("data", [])


def deal_key(d):
    return (d.get("origin"), d.get("destination"),
            d.get("departure_at", "")[:10], d.get("return_at", "")[:10])


def collect_by_country():
    """Возвращает {название страны: список дешёвых билетов (без дублей)}."""
    ret_limit = RETURN_TO[:7]
    result = {}
    for country in COUNTRIES:
        best = {}   # deal_key -> deal (храним самый дешёвый вариант каждого рейса)
        for dest in country["airports"]:
            for origin in (a.strip() for a in ITALY_AIRPORTS):
                for dep_m in (m.strip() for m in DEPART_MONTHS):
                    for ret_m in sorted({dep_m, month_after(dep_m)}):
                        if ret_m > ret_limit:
                            continue
                        for d in fetch_prices(origin, dest, dep_m, ret_m):
                            price = d.get("price")
                            if price is None or price > MAX_PRICE:
                                continue
                            if not in_window(d):
                                continue
                            k = deal_key(d)
                            if k not in best or price < best[k]["price"]:
                                best[k] = d
                        time.sleep(0.2)   # бережём rate limit
        deals = sorted(best.values(), key=lambda x: x["price"])
        result[country["name"]] = deals
        print(f"  {country['name']}: найдено {len(deals)} билетов")
    return result


# ---------------------------------------------------------------------------
# Формирование подборки
# ---------------------------------------------------------------------------

def build_digest(country, deals):
    flag = country["flag"]
    name = country["name"]
    head = (f"{flag} <b>Италия → {name}</b> · топ-{TOP_N} сейчас\n"
            f"<i>поездка {MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} дней, "
            f"окно 23 дек – 31 мар</i>")

    if not deals:
        return head + "\n\nПодходящих билетов сейчас не нашлось 🤷"

    blocks = [head]
    for i, d in enumerate(deals[:TOP_N], 1):
        origin = CITY_NAMES.get(d["origin"], d["origin"])
        dest = AIRPORT_NAMES.get(d["destination"], d["destination"])
        dep = fmt_date(d.get("departure_at", ""))
        ret = fmt_date(d.get("return_at", ""))
        days = trip_days(d)
        days_s = f", {days} дн." if days is not None else ""
        tr = f"{d.get('transfers', '?')}/{d.get('return_transfers', '?')}"
        link = build_link(d)
        block = (
            f"<b>{i}. {fmt_price(d['price'])}</b> · {origin} → {dest}\n"
            f"    {dep} – {ret}{days_s} · {tr} перес."
        )
        if link:
            block += f'\n    <a href="{html.escape(link)}">Открыть на Aviasales</a>'
        blocks.append(block)
    return "\n\n".join(blocks)


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("(!) Telegram не настроен — вывод в консоль:\n")
        print(text.replace("<b>", "").replace("</b>", "")
                  .replace("<i>", "").replace("</i>", ""))
        print("-" * 50)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! Не удалось отправить в Telegram: {e}")


def write_heartbeat(by_country):
    """Пишем короткую сводку последнего прогона (для истории и «живости» репо)."""
    data = {
        "last_run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "cheapest": {
            name: (deals[0]["price"] if deals else None)
            for name, deals in by_country.items()
        },
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  ! Не удалось записать {STATE_FILE}: {e}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not TP_TOKEN:
        raise SystemExit("Не задан TRAVELPAYOUTS_TOKEN. См. README.")

    now = datetime.now(timezone.utc)
    print(f"[{now:%Y-%m-%d %H:%M} UTC] Собираю топ-{TOP_N} по каждой стране "
          f"(поездка {MIN_TRIP_DAYS}–{MAX_TRIP_DAYS} дн., валюта {CURRENCY.upper()})...")

    by_country = collect_by_country()

    for country in COUNTRIES:
        deals = by_country.get(country["name"], [])
        send_telegram(build_digest(country, deals))
        time.sleep(0.4)

    write_heartbeat(by_country)
    print("Готово.")


if __name__ == "__main__":
    main()
