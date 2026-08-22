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
CONFIG_FILE = "farm_config_v10.json"

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
    "target_gain": 150000000,
    "workers": 1 # Default to 1 as requested
}
is_running = False
last_update_msg_id = None

stats = {
    "total_gained": 0,
    "claims_count": 0,
    "current_balance": 0,
    "start_balance": 0,
    "last_error": "None",
    "active_workers": 0,
    "speed_rpm": 0 # Rewards Per Minute
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
    except: return None

def delete_msg_after(chat_id, msg_id, delay=5):
    def run():
        time.sleep(delay)
        try: bot.delete_message(chat_id, msg_id)
        except: pass
    threading.Thread(target=run, daemon=True).start()

# ==========================================
# ULTRA TURBO WORKER (Optimized for Single Connection)
# ==========================================
def worker_loop(token, chat_id, worker_id):
    global is_running, stats
    
    print(f"[WORKER-{worker_id}] Ultra-Turbo Started.")
    with stats_lock:
        stats["active_workers"] += 1
        
    while is_running:
        try:
            ws = websocket.create_connection(
                WS_URL,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=15
            )
            
            # Login
            ws.send(msgpack.packb({"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(20):
                m = ws.recv()
                if isinstance(m, str): continue
                d = msgpack.unpackb(m, raw=False)
                if d.get("route") == "mytelLogin":
                    login_data = d.get("data", {})
                    break
            
            if not login_data or not login_data.get("ok"):
                print(f"[WORKER-{worker_id}] Login Failed.")
                time.sleep(5)
                continue
            
            with stats_lock:
                if stats["start_balance"] == 0:
                    stats["start_balance"] = login_data.get("cash", 0)
                stats["current_balance"] = login_data.get("cash", 0)
            
            # Room Bypass
            ws.send(msgpack.packb({"route": "play", "data": {"roomId": 1}, "msgId": 2}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(0.5)

            msg_id_counter = 5000
            last_gold_time = time.time()
            
            while is_running:
                # ULTRA BURST: Send 50 claims rapidly
                for _ in range(50):
                    ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": msg_id_counter}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    msg_id_counter += 1
                
                # Rapid Response Processing
                ws.settimeout(0.5)
                try:
                    for _ in range(60): # Process more messages per loop
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
                
                # Watchdog: Reconnect if no gold for 25s
                if time.time() - last_gold_time > 25:
                    break
                
                # No sleep or very minimal sleep for max speed
                time.sleep(0.01)
            
            ws.close()
        except Exception as e:
            print(f"[WORKER-{worker_id}] Error: {e}")
            time.sleep(3)
            
    with stats_lock:
        stats["active_workers"] -= 1

# ==========================================
# MONITOR
# ==========================================
def monitor_loop(chat_id):
    global is_running, stats
    last_gained = 0
    start_time = time.time()
    
    while is_running:
        time.sleep(10)
        with stats_lock:
            current_gained = stats["total_gained"]
            elapsed = (time.time() - start_time) / 60
            if elapsed > 0:
                stats["speed_rpm"] = int(current_gained / elapsed)
            
            status_text = (
                f"⚡️ *Turbo V10 (Single Connection)*\n"
                f"📈 Gained: +{current_gained:,}\n"
                f"💰 Balance: {stats['current_balance']:,}\n"
                f"🚀 Speed: ~{stats['speed_rpm']:,} gold/min\n"
                f"🎯 Target: {config['target_gain']:,}"
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
    btn = "🛑 Stop Bot" if is_running else "⚡️ Start Ultra-Turbo"
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
    bot.send_message(message.chat.id, "🔥 *Fish Hunter Turbo V10*\nOptimized for Single-Connection Speed.", reply_markup=get_menu(), parse_mode="Markdown")

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
            
            threading.Thread(target=worker_loop, args=(config["token"], chat_id, 1), daemon=True).start()
            threading.Thread(target=monitor_loop, args=(chat_id,), daemon=True).start()
            bot.answer_callback_query(call.id, "Starting Ultra-Turbo...")
        
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
                f"📊 *Current Stats*\n"
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
    print("[V10] Bot is active...")
    bot.infinity_polling()
