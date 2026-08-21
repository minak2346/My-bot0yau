import websocket
import msgpack
import json
import time
import threading
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import ssl
import os
import sys

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "farm_config.json"

WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn",
    "Accept-Language: my-MM,my;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With: com.mytel.myid"
]

# ==========================================
# BOT INITIALIZATION
# ==========================================
telebot.apihelper.CONNECT_TIMEOUT = 60
telebot.apihelper.READ_TIMEOUT = 60

bot = None
def init_bot():
    global bot
    if not TELEGRAM_BOT_TOKEN:
        print("[CRITICAL] TELEGRAM_BOT_TOKEN missing!")
        return False
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        return True
    except Exception as e:
        print(f"[ERROR] Bot init failed: {e}")
        return False

# ==========================================
# STATE
# ==========================================
config = {"owner_id": None, "token": None, "target": 150000000}
is_running = False
ws_conn = None
farm_thread = None

stats = {
    "total_gained": 0,
    "claims_count": 0,
    "current_balance": 0,
    "start_balance": 0
}
stats_lock = threading.Lock()

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config.update(json.load(f))
        except: pass

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)

load_config()

# ==========================================
# UTILS
# ==========================================
def parse_token(text):
    text = text.strip()
    if "access_token=" in text:
        try:
            return text.split("access_token=")[1].split("&")[0]
        except: return None
    return text if text.startswith("eyJ") else None

def send_update(chat_id, text):
    if not bot: return
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception as e:
        print(f"[TG] Failed to send update: {e}")

# ==========================================
# CORE FARMING LOGIC
# ==========================================
def farm_loop(token, chat_id):
    global is_running, ws_conn, stats
    
    print(f"[FARM] Starting loop for {chat_id}")
    while is_running:
        try:
            ws = websocket.create_connection(
                f"{WS_URL}?access_token={token}",
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=30
            )
            ws_conn = ws
            
            # Login
            ws.send(msgpack.packb({"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(20):
                m = ws.recv()
                d = msgpack.unpackb(m, raw=False)
                if d.get("msgId") == 1 or d.get("route") == "mytelLogin":
                    login_data = d.get("data", {})
                    break
            
            if not login_data or not login_data.get("ok"):
                send_update(chat_id, "❌ Login failed. Check token.")
                is_running = False
                break
            
            balance = login_data.get("cash", 0)
            with stats_lock:
                stats["start_balance"] = balance
                stats["current_balance"] = balance
                stats["total_gained"] = 0
                stats["claims_count"] = 0
            
            send_update(chat_id, f"✅ *Farm Started!*\n💰 Start Balance: {balance:,}\n🎯 Target: {config['target']:,}")
            
            last_msg_claims = 0
            while is_running:
                # Burst of 10 claims
                for _ in range(10):
                    ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": 0}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                
                # Listen for updates
                ws.settimeout(2.0)
                try:
                    for _ in range(20):
                        m = ws.recv()
                        d = msgpack.unpackb(m, raw=False)
                        if d.get("route") == "reloadCash" and d.get("data", {}).get("reason") == "claimItemOnline":
                            inner = d.get("data", {})
                            with stats_lock:
                                stats["total_gained"] += inner.get("changeCash", 0)
                                stats["current_balance"] = inner.get("newCash", balance)
                                stats["claims_count"] += 1
                except: pass
                
                # Send update every 10 successful claims
                with stats_lock:
                    if stats["claims_count"] >= last_msg_claims + 10:
                        last_msg_claims = stats["claims_count"]
                        msg = (
                            f"📈 *Gold Update*\n"
                            f"Claims: {stats['claims_count']}\n"
                            f"Gained: +{stats['total_gained']:,}\n"
                            f"Balance: {stats['current_balance']:,}"
                        )
                        send_update(chat_id, msg)
                        
                        if stats["current_balance"] >= config["target"]:
                            send_update(chat_id, f"🎉 *Target Reached!*\nFinal Balance: {stats['current_balance']:,}")
                            is_running = False
                            break
                
                time.sleep(0.5)
            
            ws.close()
        except Exception as e:
            print(f"[FARM] Error: {e}")
            time.sleep(5)
    
    print("[FARM] Loop ended.")

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
def get_menu():
    markup = InlineKeyboardMarkup()
    btn = "🛑 Stop Farm" if is_running else "▶️ Start Farm"
    markup.add(InlineKeyboardButton(btn, callback_data="toggle"))
    markup.add(InlineKeyboardButton("🔑 Set Token", callback_data="set_token"))
    markup.add(InlineKeyboardButton("📊 Status", callback_data="status"))
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    global config
    if config["owner_id"] is None:
        config["owner_id"] = message.chat.id
        save_config()
    bot.send_message(message.chat.id, "💰 *Gold Farm Bot (1500 Exploit)*\nStatus updates every 10 claims.", reply_markup=get_menu(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running, farm_thread
    chat_id = call.message.chat.id
    if config["owner_id"] != chat_id: return
    
    if call.data == "toggle":
        if is_running:
            is_running = False
            bot.answer_callback_query(call.id, "Stopping...")
        else:
            if not config["token"]:
                bot.answer_callback_query(call.id, "Set token first!", show_alert=True)
                return
            is_running = True
            farm_thread = threading.Thread(target=farm_loop, args=(config["token"], chat_id), daemon=True)
            farm_thread.start()
            bot.answer_callback_query(call.id, "Starting...")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_menu())
        
    elif call.data == "set_token":
        msg = bot.send_message(chat_id, "🔑 Send your Access Token or Game URL:")
        bot.register_next_step_handler(msg, process_token)
        bot.answer_callback_query(call.id)
        
    elif call.data == "status":
        with stats_lock:
            status = "🟢 Running" if is_running else "🔴 Stopped"
            text = (
                f"📊 *Farm Status*\n"
                f"State: {status}\n"
                f"Claims: {stats['claims_count']}\n"
                f"Gained: {stats['total_gained']:,}\n"
                f"Balance: {stats['current_balance']:,}"
            )
        bot.send_message(chat_id, text, parse_mode="Markdown")
        bot.answer_callback_query(call.id)

def process_token(message):
    token = parse_token(message.text)
    if token:
        config["token"] = token
        save_config()
        bot.send_message(message.chat.id, "✅ Token updated!")
    else:
        bot.send_message(message.chat.id, "❌ Invalid token.")

if __name__ == "__main__":
    if init_bot():
        print("[STARTUP] Bot is running...")
        while True:
            try:
                bot.infinity_polling(timeout=60)
            except:
                time.sleep(5)
