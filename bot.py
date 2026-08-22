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
CONFIG_FILE = "farm_config_v12.json"

WS_HEADERS = {
    "User-Agent": "Android SM-S918B",
    "Origin": "https://fishmya.ugame.vn",
    "X-Requested-With": "com.mytel.myid"
}

# ==========================================
# BOT INITIALIZATION
# ==========================================
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
config = {
    "owner_id": None, 
    "token": None, 
    "target_gain": 150000000
}
is_running = False
last_update_msg_id = None

stats = {
    "total_gained": 0,
    "claims_count": 0,
    "current_balance": 0,
    "start_balance": 0,
    "last_error": "None",
    "speed_rpm": 0
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
            try: bot.delete_message(chat_id, last_update_msg_id)
            except: pass
        
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        if auto_delete: last_update_msg_id = msg.message_id
        return msg.message_id
    except: return None

def delete_msg_after(chat_id, msg_id, delay=5):
    def run():
        time.sleep(delay)
        try: bot.delete_message(chat_id, msg_id)
        except: pass
    threading.Thread(target=run, daemon=True).start()

# ==========================================
# V12 ULTRA-TURBO WORKER (Strict User Logic)
# ==========================================
def worker_loop(token, chat_id):
    global is_running, stats
    
    print(f"[V12] Worker Started.")
    
    while is_running:
        try:
            ws = websocket.create_connection(
                WS_URL,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=30
            )
            
            # 1. Login exactly like user's pasted_content(1).txt
            login_packet = {"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1}
            ws.send(msgpack.packb(login_packet, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(60): # Wait longer for login
                m = ws.recv()
                if isinstance(m, str): continue
                d = msgpack.unpackb(m, raw=False)
                
                # Check for login response
                if d.get("msgId") == 1 or d.get("route") == "mytelLogin":
                    login_data = d.get("data", {})
                    break
            
            if not login_data or not login_data.get("ok"):
                err = login_data.get("msg", "Login Failed") if login_data else "No Response"
                print(f"[V12] Login Error: {err}")
                with stats_lock: stats["last_error"] = f"Login: {err}"
                send_update(chat_id, f"❌ *Login Failed:* {err}\n🔄 Retrying in 10s...", auto_delete=True)
                time.sleep(10)
                continue
            
            # 2. Extract Balance (Checking multiple fields)
            balance = login_data.get("cash") or login_data.get("gold") or login_data.get("balance") or 0
            with stats_lock:
                if stats["start_balance"] == 0:
                    stats["start_balance"] = balance
                stats["current_balance"] = balance
                stats["last_error"] = "None"
            
            send_update(chat_id, f"✅ *Login Successful!*\n💰 Initial Balance: {balance:,}", auto_delete=True)
            
            # 3. Enter Room (User Logic)
            ws.send(msgpack.packb({"route": "play", "data": {"roomId": 1}, "msgId": 2}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(1)

            msg_id_counter = 1000
            last_gold_time = time.time()
            
            while is_running:
                # ULTRA TURBO BURST
                for _ in range(30): # Reduced slightly for stability
                    ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": msg_id_counter}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    msg_id_counter += 1
                
                ws.settimeout(0.8)
                try:
                    for _ in range(40):
                        m = ws.recv()
                        if isinstance(m, str): continue
                        d = msgpack.unpackb(m, raw=False)
                        
                        if d.get("route") == "reloadCash":
                            inner = d.get("data", {})
                            change = inner.get("changeCash", 0)
                            if change > 0:
                                with stats_lock:
                                    stats["total_gained"] += change
                                    stats["current_balance"] = inner.get("newCash", stats["current_balance"])
                                    stats["claims_count"] += 1
                                last_gold_time = time.time()
                        
                        elif d.get("data", {}).get("ok") == False:
                            inner = d.get("data", {})
                            with stats_lock:
                                stats["last_error"] = inner.get("msg", "Action Failed")
                except websocket.WebSocketTimeoutException:
                    pass
                except: pass
                
                # Check target
                with stats_lock:
                    if stats["total_gained"] >= config["target_gain"]:
                        is_running = False
                        break
                
                # Watchdog
                if time.time() - last_gold_time > 30:
                    break
                
                time.sleep(0.1)
            
            ws.close()
        except Exception as e:
            print(f"[V12] Exception: {e}")
            with stats_lock: stats["last_error"] = str(e)
            time.sleep(5)

# ==========================================
# MONITOR
# ==========================================
def monitor_loop(chat_id):
    global is_running, stats
    start_time = time.time()
    
    while is_running:
        time.sleep(12)
        with stats_lock:
            current_gained = stats["total_gained"]
            elapsed = (time.time() - start_time) / 60
            if elapsed > 0:
                stats["speed_rpm"] = int(current_gained / elapsed)
            
            status_text = (
                f"🚀 *Turbo V12 Active*\n"
                f"📈 Gained: +{current_gained:,}\n"
                f"💰 Balance: {stats['current_balance']:,}\n"
                f"⚡️ Speed: ~{stats['speed_rpm']:,} gold/min\n"
                f"⚠️ Error: {stats['last_error']}"
            )
            send_update(chat_id, status_text, auto_delete=True)
            
            if current_gained >= config["target_gain"]:
                send_update(chat_id, f"✅ *Target Reached!*\nTotal Gained: +{current_gained:,}\nFinal Balance: {stats['current_balance']:,}")
                is_running = False

# ==========================================
# TELEGRAM
# ==========================================
def get_menu():
    markup = InlineKeyboardMarkup()
    btn = "🛑 Stop Bot" if is_running else "⚡️ Start V12 Turbo"
    markup.add(InlineKeyboardButton(btn, callback_data="toggle"))
    markup.add(InlineKeyboardButton("🔑 Set Token", callback_data="set_token"))
    markup.add(InlineKeyboardButton("🎯 Set Target", callback_data="set_target"))
    markup.add(InlineKeyboardButton("📊 Status", callback_data="status"))
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    global config
    config["owner_id"] = message.chat.id
    save_config()
    bot.send_message(message.chat.id, "🔥 *Fish Hunter Turbo V12*\nStrict Login Fix + Balance Detection.", reply_markup=get_menu(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running, stats
    chat_id = call.message.chat.id
    
    if call.data == "toggle":
        if is_running:
            is_running = False
            bot.answer_callback_query(call.id, "Stopping...")
        else:
            if not config["token"]:
                bot.answer_callback_query(call.id, "Set token first!", show_alert=True)
                return
            is_running = True
            # Reset
            with stats_lock:
                for k in stats:
                    if isinstance(stats[k], (int, float)): stats[k] = 0
                stats["last_error"] = "None"
            
            threading.Thread(target=worker_loop, args=(config["token"], chat_id), daemon=True).start()
            threading.Thread(target=monitor_loop, args=(chat_id,), daemon=True).start()
            bot.answer_callback_query(call.id, "Starting V12...")
        
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_menu())
    
    elif call.data == "set_token":
        msg = bot.send_message(chat_id, "🔑 Send Token or Game URL:")
        bot.register_next_step_handler(msg, process_token)
    
    elif call.data == "set_target":
        msg = bot.send_message(chat_id, "🎯 Enter target gain (e.g. 150000000):")
        bot.register_next_step_handler(msg, process_target)
        
    elif call.data == "status":
        with stats_lock:
            text = (
                f"📊 *Current Stats V12*\n"
                f"Gained: +{stats['total_gained']:,}\n"
                f"Balance: {stats['current_balance']:,}\n"
                f"Speed: {stats['speed_rpm']:,} gold/min\n"
                f"Error: {stats['last_error']}"
            )
        bot.send_message(chat_id, text, parse_mode="Markdown")

def process_token(message):
    token = parse_token(message.text)
    if token:
        config["token"] = token
        save_config()
        bot.send_message(message.chat.id, "✅ Token Updated.")
    else:
        bot.send_message(message.chat.id, "❌ Invalid Token.")

def process_target(message):
    try:
        val = int(message.text.replace(",", ""))
        config["target_gain"] = val
        save_config()
        bot.send_message(message.chat.id, f"🎯 Target set to {val:,}")
    except:
        bot.send_message(message.chat.id, "❌ Invalid number.")

if __name__ == "__main__":
    print("[V12] Bot is active...")
    bot.infinity_polling()
