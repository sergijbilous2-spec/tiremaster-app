import asyncio
import os
import sqlite3
from typing import List

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- НАЛАШТУВАННЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9129940332:AAHjq44i38YP_-FVuQWYLnoAhyzpCAG-6PY")
MASTER_PASSWORD = os.getenv("MASTER_PASSWORD", "1234")
DB_PATH = "tiremaster.db"

CAR_TYPES = {
    "sedan": "Легковий (R14–R16)",
    "suv": "Кросовер (R17–R18)",
    "bus": "Бус / SUV (R19+)",
}

DEFAULT_PRICES_BY_CAR = {
    "sedan": {1: 800, 2: 400, 3: 250, 4: 600, 5: 200, 6: 500},
    "suv":   {1: 1050, 2: 520, 3: 320, 4: 750, 5: 260, 6: 650},
    "bus":   {1: 1300, 2: 650, 3: 400, 4: 900, 5: 320, 6: 800},
}

SERVICE_NAMES = {
    1: "Сезонне перевзування (4 колеса)",
    2: "Фінішне балансування коліс",
    3: "Ремонт проколу шини",
    4: "Сезонне зберігання шин (місяць)",
    5: "Підкачка шин азотом",
    6: "Прокатка литих дисків",
}

STATUS_LABELS = {
    "new": "🆕 Нова заявка",
    "confirmed": "✅ Підтверджено",
    "in_progress": "🔧 В роботі",
    "done": "✔️ Виконано",
    "cancelled": "❌ Скасовано",
}


# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS masters (user_id INTEGER PRIMARY KEY)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prices_by_car (
            car_type TEXT NOT NULL,
            service_id INTEGER NOT NULL,
            price INTEGER NOT NULL,
            PRIMARY KEY (car_type, service_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            car TEXT NOT NULL,
            car_type TEXT NOT NULL DEFAULT 'sedan',
            post TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            phone TEXT NOT NULL,
            services TEXT NOT NULL,
            total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        )
    """)
    for ctype, services_map in DEFAULT_PRICES_BY_CAR.items():
        for sid, price in services_map.items():
            cur.execute(
                "INSERT OR IGNORE INTO prices_by_car (car_type, service_id, price) VALUES (?, ?, ?)",
                (ctype, sid, price),
            )
    conn.commit()
    conn.close()


def is_master(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM masters WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def add_master(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO masters (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def get_all_masters() -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM masters")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def get_prices_by_car() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT car_type, service_id, price FROM prices_by_car")
    rows = cur.fetchall()
    conn.close()

    result = {ctype: dict(services_map) for ctype, services_map in DEFAULT_PRICES_BY_CAR.items()}
    for ctype, sid, price in rows:
        if ctype in result:
            result[ctype][int(sid)] = price

    return {
        ctype: {str(sid): result[ctype].get(sid, DEFAULT_PRICES_BY_CAR[ctype][sid]) for sid in SERVICE_NAMES}
        for ctype in CAR_TYPES
    }


def update_price_by_car(car_type: str, service_id: int, new_price: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO prices_by_car (car_type, service_id, price)
        VALUES (?, ?, ?)
        ON CONFLICT(car_type, service_id) DO UPDATE SET price = excluded.price
    """, (car_type, service_id, new_price))
    conn.commit()
    conn.close()


def save_order(order: dict):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (id, car, car_type, post, slot_time, phone, services, total, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order["id"], order["car"], order.get("car_type", "sedan"),
        order["post"], order["time"], order["phone"],
        order["services"], order["total"], "new",
    ))
    conn.commit()
    conn.close()


def update_order_status(order_id: str, status: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_order(order_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, car, car_type, post, slot_time, phone, services, total, status FROM orders WHERE id = ?",
        (order_id,),
    )
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0], "car": r[1], "car_type": r[2], "post": r[3],
        "time": r[4], "phone": r[5], "services": r[6], "total": r[7], "status": r[8],
    }


def get_busy_slots() -> List[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT post, slot_time FROM orders WHERE status != 'cancelled'")
    rows = [{"post": r[0], "time": r[1]} for r in cur.fetchall()]
    conn.close()
    return rows


# --- TELEGRAM БОТ ---
waiting_password_users = set()
waiting_price_users = {}


def order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Підтвердити", callback_data=f"st:{order_id}:confirmed"),
            InlineKeyboardButton("🔧 В роботі", callback_data=f"st:{order_id}:in_progress"),
        ],
        [
            InlineKeyboardButton("✔️ Виконано", callback_data=f"st:{order_id}:done"),
            InlineKeyboardButton("❌ Скасувати", callback_data=f"st:{order_id}:cancelled"),
        ],
    ])


def format_order_text(o: dict) -> str:
    st_text = STATUS_LABELS.get(o["status"], o["status"])
    car_label = CAR_TYPES.get(o.get("car_type", "sedan"), o.get("car_type", ""))
    return (
        f"🛠 <b>Заявка #{o['id']}</b>\n"
        f"Статус: <b>{st_text}</b>\n"
        f"🚗 Авто: <b>{o['car']}</b> ({car_label})\n"
        f"📍 Пост: <b>{o['post']}</b>\n"
        f"🕒 Час: <b>сьогодні о {o['time']}</b>\n"
        f"📞 Телефон: <code>{o['phone']}</code>\n"
        f"📋 Послуги: {o['services']}\n"
        f"💰 Сума: <b>{o['total']} ₴</b>"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Це бот керування шиномонтажем TireMaster.\n"
        "• Увійти як майстер: /master\n"
        "• Змінити ціни по типах авто: /prices"
    )


async def cmd_master(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_master(uid):
        await update.message.reply_text("Ти вже авторизований як майстер ✅\nКоманда для зміни цін: /prices")
        return
    waiting_password_users.add(uid)
    await update.message.reply_text("Введіть пароль майстра:")


async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_master(uid):
        await update.message.reply_text("Спочатку увійди через /master")
        return
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"price_service:{sid}")]
        for sid, name in SERVICE_NAMES.items()
    ]
    await update.message.reply_text(
        "Обери послугу, ціну якої хочеш змінити:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if uid in waiting_password_users:
        waiting_password_users.remove(uid)
        if text == MASTER_PASSWORD:
            add_master(uid)
            await update.message.reply_text(
                "Доступ відкрито! Тепер тобі приходитимуть нові заявки.\n"
                "Змінити ціни можна командою /prices"
            )
        else:
            await update.message.reply_text("Невірний пароль. Спробуй ще раз через /master")
        return

    if uid in waiting_price_users and is_master(uid):
        sid, ctype = waiting_price_users.pop(uid)
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("Введи коректне число більше нуля (наприклад, 1100).")
            return
        new_price = int(text)
        update_price_by_car(ctype, sid, new_price)
        await update.message.reply_text(
            f"Оновлено ✅\n"
            f"Послуга: <b>{SERVICE_NAMES[sid]}</b>\n"
            f"Тип авто: <b>{CAR_TYPES[ctype]}</b>\n"
            f"Нова ціна: <b>{new_price} ₴</b>",
            parse_mode="HTML",
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data or ""

    if not is_master(uid):
        await query.answer("Немає доступу майстра", show_alert=True)
        return

    if data.startswith("price_service:"):
        sid = int(data.split(":")[1])
        prices = get_prices_by_car()
        buttons = [
            [InlineKeyboardButton(f"{clabel} — {prices[ctype][str(sid)]} ₴", callback_data=f"price_car:{sid}:{ctype}")]
            for ctype, clabel in CAR_TYPES.items()
        ]
        await query.answer()
        await query.message.reply_text(
            f"Послуга: <b>{SERVICE_NAMES[sid]}</b>\nОбери тип авто:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("price_car:"):
        _, sid_str, ctype = data.split(":")
        sid = int(sid_str)
        waiting_price_users[uid] = (sid, ctype)
        cur_p = get_prices_by_car()[ctype][str(sid)]
        await query.answer()
        await query.message.reply_text(
            f"Послуга: <b>{SERVICE_NAMES[sid]}</b>\n"
            f"Тип авто: <b>{CAR_TYPES[ctype]}</b>\n"
            f"Поточна ціна: <b>{cur_p} ₴</b>\n\n"
            f"Введи нову ціну (число в грн):",
            parse_mode="HTML",
        )

    elif data.startswith("st:"):
        _, order_id, new_status = data.split(":")
        if update_order_status(order_id, new_status):
            order = get_order(order_id)
            await query.edit_message_text(
                text=format_order_text(order),
                parse_mode="HTML",
                reply_markup=order_keyboard(order_id),
            )
            await query.answer("Статус оновлено")
        else:
            await query.answer("Заявку не знайдено", show_alert=True)


# --- FASTAPI СЕРВЕР ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class OrderIn(BaseModel):
    id: str
    car: str
    car_type: str = "sedan"
    post: str
    time: str
    phone: str
    services: str
    total: int


bot_app: Application = None


@app.on_event("startup")
async def on_startup():
    global bot_app
    init_db()
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("master", cmd_master))
    bot_app.add_handler(CommandHandler("prices", cmd_prices))
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await bot_app.initialize()
    await bot_app.start()
    asyncio.create_task(bot_app.updater.start_polling())


@app.on_event("shutdown")
async def on_shutdown():
    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


# Віддаємо сам сайт index.html
@app.get("/")
def serve_frontend():
    return FileResponse("index.html")


@app.get("/api/config")
def api_config():
    return {
        "prices_by_car": get_prices_by_car(),
        "busy_slots": get_busy_slots(),
    }


@app.get("/api/orders/{order_id}")
def api_get_order(order_id: str):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Not found")
    return order


@app.post("/api/orders")
async def api_create_order(payload: OrderIn):
    order_dict = payload.model_dump()
    order_dict["status"] = "new"
    save_order(order_dict)

    for mid in get_all_masters():
        try:
            await bot_app.bot.send_message(
                chat_id=mid,
                text=format_order_text(order_dict),
                parse_mode="HTML",
                reply_markup=order_keyboard(order_dict["id"]),
            )
        except Exception as e:
            print(f"Не вдалося надіслати майстру {mid}: {e}")

    return {"ok": True, "order": order_dict}
@app.post("/api/orders/{order_id}/cancel")
async def api_cancel_order(order_id: str, request: Request):
    data = {}
    try:
        data = await request.json()
    except Exception:
        pass

    order_dict = dict(data) if isinstance(data, dict) else {}
    order_dict["id"] = order_id
    order_dict["status"] = "cancelled"

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                ("cancelled", order_id),
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass

    cancel_text = (
        f"❌ <b>ЗАПИС СКАСОВАНО</b>\n\n"
        f"🆔 Номер: <code>{order_dict.get('id', order_id)}</code>\n"
        f"🚗 Авто: <b>{order_dict.get('car', 'не вказано')}</b>\n"
        f"📞 Телефон: <code>{order_dict.get('phone', 'не вказано')}</code>\n"
        f"🕒 Час: {order_dict.get('time', 'не вказано')}"
        + (f"\n🛠 Пост: {order_dict.get('post')}" if order_dict.get("post") else "")
        + (f"\n💰 Сума: {order_dict.get('total')} ₴" if order_dict.get("total") else "")
    )

    for mid in get_all_masters():
        try:
            await bot_app.bot.send_message(
                chat_id=mid,
                text=cancel_text,
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"Не вдалося надіслати скасування майстру {mid}: {e}")

    return {"ok": True, "id": order_id, "status": "cancelled"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))