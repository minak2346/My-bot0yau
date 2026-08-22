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
CONFIG_FILE = "farm_config_v8.json"

WS_HEADERS = {
    "User-Agent": "Android SM-S918B",
    "Origin": "https://fishmya.ugame.vn",
    "X-Requested-With": "com.mytel.myid"
}

# ==========================================
# BOT INITIALIZATION
# ==========================================
if not TELEGRAM_BOT_TOKEN:
    print("[CRITICAL] TELEGRAM_BOT_TOKEN environment variable is missing!")

try:
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    telebot.apihelper.CONNECT_TIMEOUT = 60
    telebot.apihelper.READ_TIMEOUT = 60
except Exception as e:
    print(f"[CRITICAL] Failed to initialize bot: {e}")
    sys.exit(1)

# ==========================================
# STATE
# ==========================================
config = {"owner_id": None, "token": None, "target_gain": 150000000, "workers": 3}
is_running = False
farm_threads = []
last_update_msg_id = None

stats = {
    "total_gained": 0,
    "claims_count": 0,
    "current_balance": 0,
    "start_balance": 0,
    "last_error": "None"
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

def send_update(chat_id, text, auto_delete=False):
    global last_update_msg_id
    try:
        if auto_delete and last_update_msg_id:
            try:
                bot.delete_message(chat_id, last_update_msg_id)
            except: pass
        
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        if auto_delete:
            last_update_msg_id = msg.message_id
        return msg.message_id
    except Exception as e:
        print(f"[TG] Failed to send update: {e}")
        return None

def delete_msg_after(chat_id, msg_id, delay=5):
    def run():
        time.sleep(delay)
        try:
            bot.delete_message(chat_id, msg_id)
        except: pass
    threading.Thread(target=run, daemon=True).start()

# ==========================================
# CORE FARMING LOGIC (TURBO V8 - GAIN BASED)
# ==========================================
def worker_loop(token, chat_id, worker_id):
    global is_running, stats
    
    print(f"[FARM] Worker {worker_id} started.")
    while is_running:
        ws = None
        try:
            ws = websocket.create_connection(
                WS_URL + "?access_token=" + token,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=30
            )
            
            # Login
            ws.send(msgpack.packb({"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(40):
                m = ws.recv()
                d = msgpack.unpackb(m, raw=False)
                if d.get("route") == "mytelLogin":
                    login_data = d.get("data", {})
                    break
            
            if not login_data or not login_data.get("username"):
                time.sleep(10)
                continue
            
            username = login_data.get("username")
            password = login_data.get("password")
            balance = login_data.get("cash", 0)
            
            if worker_id == 0:
                with stats_lock:
                    if stats["start_balance"] == 0:
                        stats["start_balance"] = balance
                    stats["current_balance"] = balance
                send_update(chat_id, f"✅ *Turbo Farm Started!*\n💰 Start Balance: {balance:,}\n🎯 Target Gain: {config['target_gain']:,}\n🚀 Workers: {config['workers']}", auto_delete=True)
            
            # Enter room
            ws.send(msgpack.packb({"route": "play", "data": {"playerId": username, "password": password, "index": 1}, "msgId": 2}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(1)

            msg_id_counter = 100
            last_gold_time = time.time()
            
            while is_running:
                # TURBO BURST
                for _ in range(20):
                    ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": msg_id_counter}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    msg_id_counter += 1
                
                time.sleep(0.2)
                
                # Listen for updates
                try:
                    ws.settimeout(0.1)
                    while True:
                        m = ws.recv()
                        d = msgpack.unpackb(m, raw=False)
                        
                        if d.get("route") == "reloadCash":
                            inner = d.get("data", {})
                            with stats_lock:
                                change = inner.get("changeCash", 0)
                                if change > 0:
                                    stats["total_gained"] += change
                                    stats["current_balance"] = inner.get("newCash", stats["current_balance"])
                                    stats["claims_count"] += 1
                                    last_gold_time = time.time()
                        
                        elif d.get("data", {}).get("ok") == False:
                            with stats_lock:
                                stats["last_error"] = d.get("data", {}).get("msg", "Action Failed")
                except: pass
                
                # GAIN TARGET CHECK
                if stats["total_gained"] >= config["target_gain"]:
                    if worker_id == 0:
                        send_update(chat_id, f"🎉 *Target Gain Reached!*\nTotal Gained: +{stats['total_gained']:,}\nFinal Balance: {stats['current_balance']:,}")
                    is_running = False
                    break
                
                # Reconnect if stuck
                if time.time() - last_gold_time > 40:
                    break
                    
            ws.close()
        except Exception as e:
            with stats_lock:
                stats["last_error"] = str(e)
            time.sleep(5)

def farm_manager(token, chat_id):
    global farm_threads, is_running, stats
    with stats_lock:
        stats["total_gained"] = 0
        stats["claims_count"] = 0
        stats["start_balance"] = 0
        
    farm_threads = []
    for i in range(config["workers"]):
        t = threading.Thread(target=worker_loop, args=(token, chat_id, i), daemon=True)
        t.start()
        farm_threads.append(t)
        time.sleep(2)
    
    last_claims = 0
    while is_running:
        time.sleep(10)
        with stats_lock:
            if stats["claims_count"] >= last_claims + 20:
                last_claims = stats["claims_count"]
                msg = (
                    f"📈 *Turbo Farm Update*\n"
                    f"New Gold Gained: +{stats['total_gained']:,}\n"
                    f"Target Gain: {config['target_gain']:,}\n"
                    f"Total Balance: {stats['current_balance']:,}\n"
                    f"Status: 🟢 Running ({config['workers']} Workers)"
                )
                send_update(chat_id, msg, auto_delete=True)

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
def get_menu():
    markup = InlineKeyboardMarkup()
    btn = "🛑 Stop Turbo Farm" if is_running else "🚀 Start Turbo Farm"
    markup.add(InlineKeyboardButton(btn, callback_data="toggle"))
    markup.add(InlineKeyboardButton("🔑 Set Token", callback_data="set_token"))
    markup.add(InlineKeyboardButton("📊 Status", callback_data="status"))
    markup.add(InlineKeyboardButton("⚙️ Set Workers", callback_data="set_workers"))
    markup.add(InlineKeyboardButton("🎯 Set Target Gain", callback_data="set_target"))
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    global config
    if config["owner_id"] is None:
        config["owner_id"] = message.chat.id
        save_config()
    bot.send_message(message.chat.id, "🎮 *FISH HUNTER TURBO BOT V8*\nMode: Gain-Based Multi-Farm", reply_markup=get_menu(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running
    chat_id = call.message.chat.id
    if config["owner_id"] != chat_id: return
    
    if call.data == "toggle":
        if is_running:
            is_running = False
            bot.answer_callback_query(call.id, "Stopping Turbo Farm...")
        else:
            if not config["token"]:
                bot.answer_callback_query(call.id, "Set token first!", show_alert=True)
                return
            is_running = True
            threading.Thread(target=farm_manager, args=(config["token"], chat_id), daemon=True).start()
            bot.answer_callback_query(call.id, "Starting Turbo Farm...")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_menu())
        
    elif call.data == "set_token":
        msg = bot.send_message(chat_id, "🔑 Send your Access Token or Game URL:")
        bot.register_next_step_handler(msg, process_token)
        bot.answer_callback_query(call.id)
        
    elif call.data == "set_workers":
        msg = bot.send_message(chat_id, "⚙️ How many workers? (1-10):")
        bot.register_next_step_handler(msg, process_workers)
        bot.answer_callback_query(call.id)
        
    elif call.data == "set_target":
        msg = bot.send_message(chat_id, "🎯 How much gold do you want to gain? (e.g. 150000000):")
        bot.register_next_step_handler(msg, process_target)
        bot.answer_callback_query(call.id)
        
    elif call.data == "status":
        with stats_lock:
            status = "🟢 Running" if is_running else "🔴 Stopped"
            text = (
                f"📊 *Turbo Farm Status*\n"
                f"State: {status}\n"
                f"Workers: {config['workers']}\n"
                f"Gained so far: +{stats['total_gained']:,}\n"
                f"Target Gain: {config['target_gain']:,}\n"
                f"Current Balance: {stats['current_balance']:,}\n"
                f"Last Error: {stats['last_error']}"
            )
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        delete_msg_after(chat_id, msg.message_id, 15)
        bot.answer_callback_query(call.id)

def process_token(message):
    token = parse_token(message.text)
    chat_id = message.chat.id
    try: bot.delete_message(chat_id, message.message_id)
    except: pass
    
    if token:
        config["token"] = token
        save_config()
        msg = bot.send_message(chat_id, "✅ Token updated!")
        delete_msg_after(chat_id, msg.message_id, 3)
    else:
        msg = bot.send_message(chat_id, "❌ Invalid token.")
        delete_msg_after(chat_id, msg.message_id, 3)

def process_workers(message):
    chat_id = message.chat.id
    try:
        val = int(message.text)
        if 1 <= val <= 10:
            config["workers"] = val
            save_config()
            bot.send_message(chat_id, f"✅ Workers set to {val}")
        else:
            bot.send_message(chat_id, "❌ Enter a number between 1 and 10.")
    except:
        bot.send_message(chat_id, "❌ Invalid number.")

def process_target(message):
    chat_id = message.chat.id
    try:
        val = int(message.text)
        if val > 0:
            config["target_gain"] = val
            save_config()
            bot.send_message(chat_id, f"✅ Target Gain set to +{val:,}")
        else:
            bot.send_message(chat_id, "❌ Enter a positive number.")
    except:
        bot.send_message(chat_id, "❌ Invalid number.")

if __name__ == "__main__":
    print("[STARTUP] Turbo Bot V8 (Gain-Based) is running...")
    while True:
        try:
            bot.infinity_polling(timeout=60)
        except Exception as e:
            print(f"[POLLING] Error: {e}")
            time.sleep(5)
