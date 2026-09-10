import json
import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request

TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = os.getenv("OWNER_ID", "0")
BASE_URL = os.getenv("BASE_URL", "https://tg-bot.onrender.com").rstrip("/")
SET_WEBHOOK = os.getenv("SET_WEBHOOK", "1") == "1"
DATA_FILE = os.getenv("DATA_FILE", "data.json")
TZ_NAME = os.getenv("TZ", "Europe/Moscow")

API = f"https://api.telegram.org/bot{TOKEN}"
lock = threading.Lock()


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"next_id": 1, "pending": [], "accepted": [], "rejected": [], "add_mode": {}}


DATA = load_data()


def _write(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def mutate(fn):
    with lock:
        fn(DATA)
        _write(DATA)


def api(method, **params):
    try:
        resp = requests.post(f"{API}/{method}", data=params, timeout=30)
        return resp.json()
    except Exception as e:
        print("API error:", e)
        return {"ok": False}


def send_message(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", **params)


def edit_message_text(chat_id, message_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("editMessageText", **params)


def answer_callback(callback_id, text=""):
    return api("answerCallbackQuery", callback_query_id=callback_id, text=text)


def main_keyboard():
    return {
        "keyboard": [[{"text": "ПРОВЕРКА"}, {"text": "ДОБАВИТЬ"}, {"text": "ПРИНЯТЫЕ"}]],
        "resize_keyboard": True,
    }


def done_button():
    return {"inline_keyboard": [[{"text": "Готово", "callback_data": "done_add"}]]}


def empty_keyboard():
    return {"inline_keyboard": []}


def is_admin(user_id):
    return OWNER_ID == "0" or str(user_id) == str(OWNER_ID)


def now_str():
    return datetime.now(ZoneInfo(TZ_NAME)).strftime("%d.%m.%Y %H:%M")


def normalize_username(raw):
    t = raw.strip().replace("https://", "").replace("http://", "").replace("t.me/", "")
    t = t.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", t):
        return None
    return "@" + t


def show_next(chat_id, message_id=None):
    if not DATA["pending"]:
        text = "Все заявки проверены ✅"
        if message_id is not None:
            edit_message_text(chat_id, message_id, text, empty_keyboard())
        else:
            send_message(chat_id, text, reply_markup=main_keyboard())
        return
    item = DATA["pending"][0]
    text = f"📋 На проверку:\n{item['user']}\nОтправлено: {item['added']}"
    kb = {
        "inline_keyboard": [
            [
                {"text": "ПРИНЯТЬ", "callback_data": f"acc:{item['id']}"},
                {"text": "ОТКЛОНИТЬ", "callback_data": f"rej:{item['id']}"},
            ]
        ]
    }
    if message_id is not None:
        edit_message_text(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def show_accepted(chat_id):
    accepted = list(DATA["accepted"])
    if not accepted:
        send_message(chat_id, "Пока никто не принят.", reply_markup=main_keyboard())
        return
    lines = [f"{i + 1}. {a['user']} — {a['time']}" for i, a in enumerate(accepted)]
    first = True
    for i in range(0, len(lines), 20):
        chunk = "\n".join(lines[i:i + 20])
        if first:
            send_message(chat_id, "ПРИНЯТЫЕ ✅:\n" + chunk, reply_markup=main_keyboard())
            first = False
        else:
            send_message(chat_id, chunk)


def do_add(chat_id, user_id, text):
    name = normalize_username(text)
    if name is None:
        send_message(chat_id, "Не похоже на юзернейм. Пример: @username")
        return
    found = {"exists": False}

    def _add(d):
        if any(p["user"] == name for p in d["pending"]):
            found["exists"] = True
            return
        d["pending"].append({"id": d["next_id"], "user": name, "added": now_str()})
        d["next_id"] += 1

    mutate(_add)
    if found["exists"]:
        send_message(chat_id, f"{name} уже в очереди на проверку.")
        return
    entry = DATA["pending"][-1]
    send_message(
        chat_id,
        f"Добавлено: {entry['user']}\nОтправлено: {entry['added']}\n\nОтправь ещё или нажми «Готово».",
        reply_markup=done_button(),
    )


def on_message(update):
    msg = update.get("message", {})
    text = (msg.get("text") or "").strip()
    chat_id = msg["chat"]["id"]
    user_id = str(msg["from"]["id"])

    if text == "/start":
        send_message(
            chat_id,
            "Привет!\n\n"
            "• ДОБАВИТЬ — отправить юзернейм на проверку\n"
            "• ПРОВЕРКА — принять или отклонить очередь\n"
            "• ПРИНЯТЫЕ — список принятых с временем\n\n"
            f"Твой Telegram ID: {user_id}",
            reply_markup=main_keyboard(),
        )
        return

    if text == "/myid":
        send_message(chat_id, f"Твой Telegram ID: {user_id}", reply_markup=main_keyboard())
        return

    if text == "/stats" and is_admin(user_id):
        send_message(
            chat_id,
            f"Очередь: {len(DATA['pending'])}\n"
            f"Принято: {len(DATA['accepted'])}\n"
            f"Отклонено: {len(DATA['rejected'])}",
        )
        return

    if text == "ПРОВЕРКА":
        mutate(lambda d: d["add_mode"].update({user_id: False}))
        if not is_admin(user_id):
            send_message(chat_id, "У тебя нет доступа к этому разделу.")
            return
        show_next(chat_id)
        return

    if text == "ДОБАВИТЬ":
        mutate(lambda d: d["add_mode"].update({user_id: True}))
        send_message(chat_id, "Отправь юзернейм на проверку.\nНапример: @username", reply_markup=done_button())
        return

    if text == "ПРИНЯТЫЕ":
        mutate(lambda d: d["add_mode"].update({user_id: False}))
        if not is_admin(user_id):
            send_message(chat_id, "У тебя нет доступа к этому разделу.")
            return
        show_accepted(chat_id)
        return

    if DATA["add_mode"].get(user_id) or text.startswith("@"):
        do_add(chat_id, user_id, text)
        return

    send_message(chat_id, "Чтобы отправить юзернейм на проверку, нажми ДОБАВИТЬ.", reply_markup=main_keyboard())


def on_callback(update):
    cq = update["callback_query"]
    cb_id = cq["id"]
    user_id = str(cq["from"]["id"])
    msg = cq["message"]
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    cdata = cq["data"]

    if cdata == "done_add":
        mutate(lambda d: d["add_mode"].update({user_id: False}))
        edit_message_text(chat_id, mid, "Готово ✅", empty_keyboard())
        answer_callback(cb_id)
        return

    if cdata.startswith(("acc:", "rej:")):
        if not is_admin(user_id):
            answer_callback(cb_id, "Нет доступа")
            return
        action, iid = cdata.split(":", 1)
        result = {}

        def _vote(d):
            idx = next((i for i, p in enumerate(d["pending"]) if str(p["id"]) == iid), None)
            if idx is None:
                result["none"] = True
                return
            item = d["pending"].pop(idx)
            ts = now_str()
            if action == "acc":
                d["accepted"].append({"user": item["user"], "time": ts})
                result["accepted"] = True
            else:
                d["rejected"].append({"user": item["user"], "time": ts})
                result["rejected"] = True
            result["item"] = item
            result["time"] = ts

        mutate(_vote)
        if result.get("none"):
            answer_callback(cb_id, "Уже обработано")
            return
        item = result["item"]
        ts = result["time"]
        answer_callback(cb_id, "Готово")
        if result.get("accepted"):
            edit_message_text(chat_id, mid, f"✅ Принят: {item['user']}\nВремя: {ts}")
        else:
            edit_message_text(chat_id, mid, f"❌ Отклонён: {item['user']}")
        show_next(chat_id, mid)
        return

    answer_callback(cb_id, "Неизвестная кнопка")


def handle(update):
    if "callback_query" in update:
        on_callback(update)
    elif "message" in update:
        on_message(update)


app = Flask(__name__)


@app.route("/", methods=["POST"])
def webhook():
    handle(request.get_json(force=True))
    return "ok"


@app.route("/", methods=["GET"])
def index():
    return "ok", 200


if SET_WEBHOOK:
    if not TOKEN:
        print("BOT_TOKEN is not set! Add it to Render environment.")
    else:
        res = api("setWebhook", url=BASE_URL + "/")
        print("setWebhook:", res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)