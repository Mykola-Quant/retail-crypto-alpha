#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_bot.py
===========
Телеграм-бот для risk-tool. Реагує на команди/кнопки:
  /chart  (або кнопка «📊 Графік»)  -> генерує і шле графік
                                       «Ціна + цільова позиція VT+TREND»
  /signal (або кнопка «🎯 Сигнал»)   -> шле текстовий денний сигнал
  /start                            -> показує кнопки

Чистий Telegram HTTP API через requests (без залежності від версії
python-telegram-bot). Перевикористовує логіку daily_risk_signal.py.

Запуск (тримай увімкненим, як колектори):
  caffeinate -i python risk_bot.py            # токен/чат з .env
  caffeinate -i python risk_bot.py --token <BOT_TOKEN>

Потрібен requests:  pip install requests
btc_daily.csv має бути свіжим (онови fetch_daily.py перед/періодично).
"""
from __future__ import annotations
import argparse, json, os, sys, time
import requests
import daily_risk_signal as drs

TG = "https://api.telegram.org/bot{token}/{method}"
KEYBOARD = {"keyboard": [["📊 Графік", "🎯 Сигнал"]],
            "resize_keyboard": True}


# --- генерація контенту (перевикористовує daily_risk_signal) -----------------
def build(file, target_vol, vol_win, sma, cost_bps, chart_path="bot_chart.png"):
    close = drs.load_daily(file)
    df = drs.compute(close, target_vol, vol_win, sma)
    eq = drs.equity_curves(df, cost_bps)
    text = drs.today_line(df)
    drs.make_chart(df, eq, chart_path)
    return text, chart_path


# --- маршрутизація команд (чиста функція -> легко тестувати без мережі) ------
def route(text: str) -> str:
    t = (text or "").strip().lower()
    if t in ("/start", "/menu", "start"):
        return "menu"
    if t.startswith("/chart") or "графік" in t:
        return "chart"
    if t.startswith("/signal") or "сигнал" in t:
        return "signal"
    return "unknown"


# --- Telegram I/O ------------------------------------------------------------
def api(token, method, **kw):
    return requests.post(TG.format(token=token, method=method), timeout=30, **kw)


def send_message(token, chat, text, keyboard=False):
    data = {"chat_id": chat, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps(KEYBOARD)
    api(token, "sendMessage", data=data)


def send_photo(token, chat, path, caption=""):
    with open(path, "rb") as f:
        api(token, "sendPhoto", data={"chat_id": chat, "caption": caption},
            files={"photo": f})


def read_env(key):
    if key in os.environ:
        return os.environ[key]
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="btc_daily.csv")
    ap.add_argument("--target-vol", type=float, default=0.20)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--sma", type=int, default=200)
    ap.add_argument("--cost-bps", type=float, default=13.0)
    ap.add_argument("--token"); ap.add_argument("--chat")
    a = ap.parse_args(argv)

    token = a.token or read_env("TELEGRAM_BOT_TOKEN")
    if not token:
        print("!! нема TELEGRAM_BOT_TOKEN (у .env або --token)"); return 1

    cfg = dict(file=a.file, target_vol=a.target_vol, vol_win=a.vol_win,
               sma=a.sma, cost_bps=a.cost_bps)

    print("Бот запущено. Команди: /chart, /signal, /start. Ctrl+C — стоп.")
    offset = None
    while True:
        try:
            r = requests.get(TG.format(token=token, method="getUpdates"),
                             params={"offset": offset, "timeout": 30}, timeout=40)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if not chat:
                    continue
                action = route(msg.get("text", ""))
                if action == "menu":
                    send_message(token, chat,
                                 "Risk-tool. Обери:\n📊 Графік — ціна+позиція\n"
                                 "🎯 Сигнал — скільки тримати завтра", keyboard=True)
                elif action == "signal":
                    text, _ = build(**cfg)
                    send_message(token, chat, text, keyboard=True)
                elif action == "chart":
                    send_message(token, chat, "Будую графік...")
                    text, path = build(**cfg)
                    send_photo(token, chat, path,
                               caption="Ціна + цільова позиція VT+TREND")
                    send_message(token, chat, text, keyboard=True)
                else:
                    send_message(token, chat,
                                 "Не зрозумів. /chart, /signal або /start",
                                 keyboard=True)
        except KeyboardInterrupt:
            print("\nЗупинено."); return 0
        except Exception as e:
            print(f"[помилка циклу, продовжую] {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
