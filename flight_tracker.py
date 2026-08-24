#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Трекер вкусных билетов Италия -> Шри-Ланка / Таиланд.

Опрашивает Aviasales/Travelpayouts API по крупным аэропортам Италии
на направления в Таиланд и Шри-Ланку, ведёт историю цен по каждому
маршруту и присылает в Telegram только реально выгодные варианты:
  * новый исторический минимум маршрута,
  * цена заметно ниже обычной (медианы истории),
  * либо просто очень дёшево по абсолютному порогу.

Запуск: python flight_tracker.py
Все секреты и настройки читаются из переменных окружения (см. README).
"""

import os
import json
import time
import html
import statistics
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
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # Значения из .env не перетирают уже заданные переменные окружения.
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


_load_dotenv()

# ---------------------------------------------------------------------------
# Настройки (можно переопределить переменными окружения)
# ---------------------------------------------------------------------------

# Токен Travelpayouts (обязательно). Получить: https://www.travelpayouts.com/
TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()

# Telegram (обязательно, если нужны уведомления)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Партнёрский маркер Travelpayouts (необязательно) — подставляется в ссылки.
TP_MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "").strip()

# Валюта. По умолчанию евро (можно "rub", "usd" и т.д.).
CURRENCY = os.environ.get("CURRENCY", "eur")
CURRENCY_SIGN = {"eur": "€", "rub": "₽", "usd": "$"}.get(CURRENCY, CURRENCY.upper())

# --- Логика "вкусной" цены -------------------------------------------------
# Абсолютный потолок здравого смысла: дороже этого вообще не рассматриваем.
MAX_PRICE = int(os.environ.get("MAX_PRICE", "650"))
# Всегда оповещать, если цена ниже этого (это уже точно отличная цена).
GREAT_DEAL_PRICE = int(os.environ.get("GREAT_DEAL_PRICE", "420"))
# Оповещать, если цена на столько ниже обычной (медианы истории). 0.12 = -12%.
DROP_PCT = float(os.environ.get("DROP_PCT", "0.12"))
# Сколько точек истории нужно, чтобы доверять медиане.
MIN_HISTORY = int(os.environ.get("MIN_HISTORY", "4"))
# Сколько последних наблюдений хранить на маршрут.
HISTORY_CAP = int(os.environ.get("HISTORY_CAP", "200"))

# --- Окно дат поездки ------------------------------------------------------
# Вылет не раньше DEPART_FROM, возврат не позже RETURN_TO.
DEPART_FROM = os.environ.get("DEPART_FROM", "2026-12-23")
RETURN_TO = os.environ.get("RETURN_TO", "2027-03-31")

# Длительность поездки (в днях): берём только билеты с таким сроком туда-обратно.
MIN_TRIP_DAYS = int(os.environ.get("MIN_TRIP_DAYS", "12"))
MAX_TRIP_DAYS = int(os.environ.get("MAX_TRIP_DAYS", "16"))

# Месяцы вылета, которые запрашиваем у API (через запятую, YYYY-MM).
# Месяцы возврата подбираются автоматически (тот же месяц и следующий),
# потому что короткая поездка укладывается максимум в два соседних месяца.
DEPART_MONTHS = os.environ.get(
    "DEPART_MONTHS", "2026-12,2027-01,2027-02,2027-03"
).split(",")

# Сколько вариантов запрашивать за один запрос (больше = больше дат для выбора).
LIMIT = int(os.environ.get("LIMIT", "100"))

# Файл истории цен (используется и для антиспама).
STATE_FILE = os.environ.get("STATE_FILE", "history.json")

# Аэропорты вылета в Италии (IATA-коды крупных аэропортов).
ITALY_AIRPORTS = os.environ.get(
    "ITALY_AIRPORTS",
    "FCO,MXP,BGY,VCE,BLQ,NAP,TRN,PSA,CTA,BRI,VRN,FLR"
).split(",")

# Направления: код -> человекочитаемое имя.
DESTINATIONS = {
    "CMB": "Шри-Ланка · Коломбо",
    "BKK": "Таиланд · Бангкок",
    "DMK": "Таиланд · Бангкок (Дон Муанг)",
    "HKT": "Таиланд · Пхукет",
    "CNX": "Таиланд · Чиангмай",
    "USM": "Таиланд · Самуи",
    "KBV": "Таиланд · Краби",
}

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

MONTHS_RU = ["", "янв", "фев", "мар", "апр", "мая", "июн",
             "июл", "авг", "сен", "окт", "ноя", "дек"]
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt_date(iso):
    """'2027-01-11' -> '11 янв (сб)'."""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso[:10]
    return f"{d.day} {MONTHS_RU[d.month]} ({WEEKDAYS_RU[d.weekday()]})"

CITY_NAMES = {
    # Коды аэропортов
    "FCO": "Рим", "MXP": "Милан", "BGY": "Бергамо", "VCE": "Венеция",
    "BLQ": "Болонья", "NAP": "Неаполь", "TRN": "Турин", "PSA": "Пиза",
    "CTA": "Катания", "BRI": "Бари", "VRN": "Верона", "FLR": "Флоренция",
    # Коды городов, которые API возвращает вместо аэропортов
    "ROM": "Рим", "MIL": "Милан",
}


# ---------------------------------------------------------------------------
# Работа с историей / состоянием
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def route_id(origin, destination):
    return f"{origin}-{destination}"


def deal_key(deal):
    """Уникальный ключ конкретного предложения (маршрут + даты)."""
    return f"{deal['origin']}-{deal['destination']}-{deal['departure_at'][:10]}-{deal['return_at'][:10]}"


# ---------------------------------------------------------------------------
# Запрос к API
# ---------------------------------------------------------------------------

def fetch_prices(origin, destination, depart_month, return_month):
    """Запрашивает кэш цен туда-обратно по одному маршруту и паре месяцев."""
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
    except requests.RequestException as e:
        print(f"  ! Ошибка запроса {origin}->{destination} "
              f"({depart_month}/{return_month}): {e}")
        return []
    except ValueError:
        print(f"  ! Некорректный JSON для {origin}->{destination}")
        return []

    if not payload.get("success", False):
        return []
    return payload.get("data", [])


def trip_days(deal):
    """Длительность поездки в днях, или None если даты кривые."""
    dep = deal.get("departure_at", "")[:10]
    ret = deal.get("return_at", "")[:10]
    try:
        return (date.fromisoformat(ret) - date.fromisoformat(dep)).days
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
    """'2026-12' -> '2027-01'."""
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
# Сбор всех предложений в окне
# ---------------------------------------------------------------------------

def collect_raw():
    """Возвращает dict: route_id -> список подходящих предложений (в окне, <= MAX_PRICE)."""
    by_route = {}
    ret_limit = RETURN_TO[:7]   # не запрашиваем месяцы возврата позже окна
    for origin in (a.strip() for a in ITALY_AIRPORTS):
        for dest in DESTINATIONS:
            for dep_m in (m.strip() for m in DEPART_MONTHS):
                # короткая поездка укладывается в текущий месяц или переходит в следующий
                for ret_m in sorted({dep_m, month_after(dep_m)}):
                    if ret_m > ret_limit:
                        continue
                    raw = fetch_prices(origin, dest, dep_m, ret_m)
                    for d in raw:
                        price = d.get("price")
                        if price is None or price > MAX_PRICE:
                            continue
                        if not in_window(d):
                            continue
                        by_route.setdefault(route_id(origin, dest), []).append(d)
                    time.sleep(0.25)  # бережём rate limit API
    return by_route


# ---------------------------------------------------------------------------
# Анализ: что считать "вкусным"
# ---------------------------------------------------------------------------

def analyze(by_route, state):
    """
    Обновляет историю и возвращает список находок для отправки.
    Каждая находка: (deal, baseline, discount, label).
    """
    alerts = []

    for rid, deals in by_route.items():
        node = state.setdefault(rid, {"prices": [], "notified": {}})
        past_prices = node["prices"]
        baseline = statistics.median(past_prices) if len(past_prices) >= MIN_HISTORY else None
        past_min = min(past_prices) if past_prices else None

        # Дешёвые сначала.
        deals.sort(key=lambda x: x["price"])

        for d in deals:
            price = d["price"]
            key = deal_key(d)

            # Причина, по которой предложение "вкусное".
            label = None
            if past_min is not None and price < past_min:
                label = "📉 Новый минимум маршрута"
            elif baseline is not None and price <= baseline * (1 - DROP_PCT):
                label = "🔥 Заметно дешевле обычного"
            elif price <= GREAT_DEAL_PRICE:
                label = "💎 Отличная цена"
            elif baseline is None and past_min is None and price <= GREAT_DEAL_PRICE:
                label = "💎 Отличная цена"

            if label is None:
                continue

            # Антиспам: не слать то же предложение по той же (или большей) цене.
            last = node["notified"].get(key)
            if last is not None and price >= last:
                continue

            discount = None
            if baseline:
                discount = (baseline - price) / baseline

            alerts.append((d, baseline, discount, label))
            node["notified"][key] = price

        # Записываем в историю лучшую цену этого прогона по маршруту.
        if deals:
            node["prices"].append(deals[0]["price"])
            if len(node["prices"]) > HISTORY_CAP:
                node["prices"] = node["prices"][-HISTORY_CAP:]

    # Самые выгодные (по величине скидки, затем по цене) — наверх.
    alerts.sort(key=lambda t: (-(t[2] or 0), t[0]["price"]))
    return alerts


# ---------------------------------------------------------------------------
# Форматирование и Telegram
# ---------------------------------------------------------------------------

def fmt_price(p):
    return f"{p:,}".replace(",", " ") + f" {CURRENCY_SIGN}"


def format_deal(deal, baseline, discount, label):
    origin = deal["origin"]
    city = CITY_NAMES.get(origin, origin)
    dest_name = DESTINATIONS.get(deal["destination"], deal["destination"])
    dep = fmt_date(deal.get("departure_at", ""))
    ret = fmt_date(deal.get("return_at", ""))
    airline = deal.get("airline", "?")
    transfers = deal.get("transfers", "?")
    ret_transfers = deal.get("return_transfers", "?")
    link = build_link(deal)

    text = (
        f"{label}\n"
        f"✈️ <b>{fmt_price(deal['price'])}</b> — {city} ({origin}) → {dest_name}\n"
    )
    if baseline:
        ctx = f"обычно ~{fmt_price(round(baseline))}"
        if discount:
            ctx += f" · сейчас −{round(discount * 100)}%"
        text += f"📊 {ctx}\n"
    days = trip_days(deal)
    nights = f"  •  {days} дн." if days is not None else ""
    text += (
        f"📅 Туда: {dep}  •  Обратно: {ret}{nights}\n"
        f"🛫 {html.escape(str(airline))}  •  пересадки: {transfers}/{ret_transfers}\n"
    )
    if link:
        text += f'🔗 <a href="{html.escape(link)}">Открыть на Aviasales</a>'
    return text


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("(!) Telegram не настроен — вывод в консоль:\n")
        print(text.replace("<b>", "").replace("</b>", ""))
        print("-" * 50)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! Не удалось отправить в Telegram: {e}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not TP_TOKEN:
        raise SystemExit("Не задан TRAVELPAYOUTS_TOKEN. См. README.")

    now = datetime.now(timezone.utc)
    print(f"[{now:%Y-%m-%d %H:%M} UTC] Ищу вкусные билеты "
          f"(вылет с {DEPART_FROM}, возврат до {RETURN_TO}, валюта {CURRENCY.upper()})...")

    state = load_state()
    by_route = collect_raw()
    total = sum(len(v) for v in by_route.values())
    print(f"Собрано предложений в окне (<= {MAX_PRICE} {CURRENCY_SIGN}): {total}")

    alerts = analyze(by_route, state)

    if not alerts:
        print("Вкусных вариантов пока нет (или они уже отправлялись).")
    else:
        print(f"Отправляю находок: {len(alerts)}")
        send_telegram(f"🎯 Нашёл выгодные билеты: {len(alerts)}\n" + "—" * 18)
        for deal, baseline, discount, label in alerts:
            send_telegram(format_deal(deal, baseline, discount, label))
            time.sleep(0.3)

    save_state(state)
    print("Готово.")


if __name__ == "__main__":
    main()
