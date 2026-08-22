Import websocket
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
CONFIG_FILE = "farm_config_v6.json"

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
config = {"owner_id": None, "token": None, "target": 15000000000}
is_running = False
ws_conn = None
farm_thread = None
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
# CORE FARMING LOGIC
# ==========================================
def farm_loop(token, chat_id):
    global is_running, ws_conn, stats
    
    print(f"[FARM] Starting loop for {chat_id}")
    while is_running:
        try:
            ws = websocket.create_connection(
                WS_URL,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=30
            )
            ws_conn = ws
            
            # Login
            ws.send(msgpack.packb({"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(40):
                m = ws.recv()
                d = msgpack.unpackb(m, raw=False)
                if d.get("msgId") == 1:
                    login_data = d.get("data", {})
                    break
            
            # ၁။ Login Failed ဖြစ်ရင် ရပ်မသွားဘဲ Auto ပြန်စအောင် ပြင်ထားခြင်း
            if not login_data or not login_data.get("ok"):
                err_msg = login_data.get("msg", "Unknown Login Error") if login_data else "No Response"
                send_update(chat_id, f"❌ *Login Failed*\nReason: {err_msg}\n🔄 စက္ကန့် ၃၀ အကြာတွင် Auto ပြန်စပါမည်...", auto_delete=True)
                time.sleep(30) # ခဏနားပြီး
                continue # အစကနေ Auto ပြန်ချိတ်ပါမယ်
            
            balance = login_data.get("cash", 0)
            with stats_lock:
                stats["start_balance"] = balance
                stats["current_balance"] = balance
                stats["total_gained"] = 0
                stats["claims_count"] = 0
            
            send_update(chat_id, f"✅ *Farm Started!*\n💰 Current Balance: {balance:,}\n🎯 Target: {config['target']:,}")
            
            # CRITICAL: Enter room to bypass "Silent Block"
            ws.send(msgpack.packb({"route": "play", "data": {"roomId": 1}, "msgId": 2}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(1)

            last_msg_claims = 0
            msg_id_counter = 100
            
            # ၃။ Gold မတက်တာကို စစ်ဆေးရန် အချိန်မှတ်ထားခြင်း
            last_gold_time = time.time()
            
            while is_running:
                # Burst of 10 claims
                for _ in range(10):
                    ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": msg_id_counter}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    msg_id_counter += 1
                
                # Listen for updates
                ws.settimeout(2.0)
                try:
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
                                    # Gold တက်ရင် အချိန်ကို အသစ်ပြန်မှတ်ပါမယ်
                                    last_gold_time = time.time()
                        
                        elif d.get("data", {}).get("ok") == False:
                            inner = d.get("data", {})
                            with stats_lock:
                                stats["last_error"] = inner.get("msg", "Action Failed")
                except websocket.WebSocketTimeoutException:
                    pass
                except: pass
                
                # ၃။ စက္ကန့် ၃၀ အတွင်း Gold လုံးဝ မတက်ရင် Loop ကိုဖြတ်ပြီး Auto အစကနေ ပြန်စပါမယ်
                if time.time() - last_gold_time > 30:
                    send_update(chat_id, "⚠️ *Gold ရပ်နေပါသည်*\n🔄 Auto အစကနေ ပြန်စနေပါသည်...", auto_delete=True)
                    break
                
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
                        send_update(chat_id, msg, auto_delete=True)
                        
                        if stats["current_balance"] >= config["target"]:
                            send_update(chat_id, f"🎉 *Target Reached!*\nFinal Balance: {stats['current_balance']:,}")
                            is_running = False
                            break
                
                time.sleep(0.5)
            
            ws.close()
        # ၂။ Error တစ်ခုခုဖြစ်ရင် (Network ကျတာမျိုး) ၅ စက္ကန့်နေရင် Auto ပြန်ချိတ်ပါမယ်
        except Exception as e:
            print(f"[FARM] Error: {e}. Reconnecting in 5s...")
            with stats_lock:
                stats["last_error"] = str(e)
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
    bot.send_message(message.chat.id, "💰 *Gold Farm Bot V6 (Room Bypass)*\nExploit: claimItemOnline in room.", reply_markup=get_menu(), parse_mode="Markdown")

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
                f"Balance: {stats['current_balance']:,}\n"
                f"Last Error: {stats['last_error']}"
            )
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        delete_msg_after(chat_id, msg.message_id, 10)
        bot.answer_callback_query(call.id)

def process_token(message):
    token = parse_token(message.text)
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except: pass
    
    if token:
        config["token"] = token
        save_config()
        msg = bot.send_message(chat_id, "✅ Token updated!")
        delete_msg_after(chat_id, msg.message_id, 3)
    else:
        msg = bot.send_message(chat_id, "❌ Invalid token.")
        delete_msg_after(chat_id, msg.message_id, 3)

if __name__ == "__main__":
    print("[STARTUP] Bot V6 is running...")
    while True:
        try:
            bot.infinity_polling(timeout=60)
        except Exception as e:
            print(f"[POLLING] Error: {e}")
            time.sleep(5)
