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
import random
import logging

sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# LOGGING
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8875811759:AAEC_VPIoThZh_yYrkbnzgKBTTQv17roqs4"
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "farm_config_fixed.json"

WS_HEADERS = {
    "User-Agent": "Android SM-S918B",
    "Origin": "https://fishmya.ugame.vn",
    "X-Requested-With": "com.mytel.myid"
}

# ==========================================
# BOT
# ==========================================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

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
    "start_balance": 0,
    "last_error": "None",
    "success_rate": 0.0,
    "current_burst": 80,
    "current_package": 3
}
stats_lock = threading.Lock()

# ==========================================
# FILE OPS
# ==========================================
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config.update(json.load(f))
        except:
            pass

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

load_config()

# ==========================================
# RATE LIMITER (FIXED)
# ==========================================
class FixedRateLimiter:
    def __init__(self):
        self.burst_size = 80  # ← 150 ကနေ 80 ကိုလျှော့
        self.package_size = 3  # ← 5 ကနေ 3 ကိုလျှော့
        self.min_burst = 30
        self.max_burst = 120
        self.min_package = 2
        self.max_package = 8
        self.success_count = 0
        self.fail_count = 0
        self.success_rate = 0.0
        self.adaptive_enabled = False  # ← Adaptive ကိုပိတ်ထား
    
    def adjust(self):
        if not self.adaptive_enabled:
            return
        
        total = self.success_count + self.fail_count
        if total == 0:
            return
        
        self.success_rate = self.success_count / total
        
        if self.success_rate > 0.7:
            self.burst_size = min(self.burst_size + 5, self.max_burst)
        elif self.success_rate < 0.3:
            self.burst_size = max(self.burst_size - 10, self.min_burst)
        
        self.success_count = 0
        self.fail_count = 0
        
        with stats_lock:
            stats["success_rate"] = self.success_rate
            stats["current_burst"] = self.burst_size
            stats["current_package"] = self.package_size

rate_limiter = FixedRateLimiter()

# ==========================================
# UTILS
# ==========================================
def parse_token(text):
    text = text.strip()
    if "access_token=" in text:
        try:
            return text.split("access_token=")[1].split("&")[0]
        except:
            return None
    return text if text.startswith("eyJ") else None

def send_update(chat_id, text):
    try:
        return bot.send_message(chat_id, text, parse_mode="Markdown")
    except:
        return None

def delete_msg_after(chat_id, msg_id, delay=5):
    def run():
        time.sleep(delay)
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
    threading.Thread(target=run, daemon=True).start()

# ==========================================
# CORE FARMING LOGIC (FIXED)
# ==========================================
def farm_loop(token, chat_id):
    global is_running, ws_conn, stats
    
    logger.info(f"🚀 Starting farm loop for {chat_id}")
    
    while is_running:
        try:
            # ========== CONNECT ==========
            ws = websocket.create_connection(
                WS_URL,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=30
            )
            ws_conn = ws
            logger.info("✅ WebSocket connected")
            
            # ========== LOGIN ==========
            ws.send(msgpack.packb({
                "route": "mytelLogin", 
                "data": {"accessToken": token, "language": "my"}, 
                "msgId": 1
            }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(40):
                try:
                    m = ws.recv()
                    d = msgpack.unpackb(m, raw=False)
                    if d.get("msgId") == 1:
                        login_data = d.get("data", {})
                        break
                except:
                    pass
            
            if not login_data or not login_data.get("ok"):
                logger.warning("Login failed, reconnecting...")
                time.sleep(5)
                continue
            
            balance = login_data.get("cash", 0)
            with stats_lock:
                stats["start_balance"] = balance
                stats["current_balance"] = balance
                stats["total_gained"] = 0
                stats["claims_count"] = 0
                stats["last_error"] = "None"
            
            logger.info(f"💰 Starting balance: {balance:,}")
            
            # ========== JOIN ROOM ==========
            ws.send(msgpack.packb({
                "route": "play", 
                "data": {"roomId": 1}, 
                "msgId": 2
            }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(2)
            
            # ========== FARM LOOP ==========
            msg_id_counter = 100
            last_gold_time = time.time()
            consecutive_failures = 0
            
            while is_running:
                # ----- SEND BURST -----
                current_burst = rate_limiter.burst_size
                current_package = rate_limiter.package_size
                
                for _ in range(current_burst):
                    try:
                        # Random package (2-8)
                        package = random.randint(current_package - 1, current_package + 1)
                        package = max(rate_limiter.min_package, min(rate_limiter.max_package, package))
                        
                        ws.send(msgpack.packb({
                            "route": "claimItemOnline", 
                            "data": {"package": package}, 
                            "msgId": msg_id_counter
                        }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                        msg_id_counter += 1
                        
                        # Small delay
                        time.sleep(0.005)
                    except Exception as e:
                        logger.error(f"Send error: {e}")
                        consecutive_failures += 1
                        break
                
                if consecutive_failures > 3:
                    logger.warning("Too many failures, reconnecting...")
                    break
                
                # ----- READ RESPONSES -----
                success_count = 0
                ws.settimeout(1.5)  # ← 1.0 ကနေ 1.5 ကိုတိုး
                
                try:
                    while True:
                        try:
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
                                        success_count += 1
                                        last_gold_time = time.time()
                                        consecutive_failures = 0
                            
                            elif d.get("data", {}).get("ok") == False:
                                inner = d.get("data", {})
                                with stats_lock:
                                    stats["last_error"] = inner.get("msg", "Action Failed")
                                consecutive_failures += 1
                                
                        except websocket.WebSocketTimeoutException:
                            break
                        except Exception as e:
                            logger.error(f"Response error: {e}")
                            break
                            
                except Exception as e:
                    logger.error(f"Response reading error: {e}")
                
                # ----- UPDATE STATS -----
                rate_limiter.success_count += success_count
                rate_limiter.fail_count += current_burst - success_count
                
                # ----- CHECK STATUS -----
                if time.time() - last_gold_time > 20:  # ← 15 ကနေ 20 ကိုတိုး
                    logger.warning("No gold for 20s, reconnecting...")
                    with stats_lock:
                        stats["last_error"] = "No gold received"
                    break
                
                with stats_lock:
                    if stats["current_balance"] >= config["target"]:
                        logger.info(f"🎉 Target reached! {stats['current_balance']:,}")
                        send_update(chat_id, f"🎉 *Target Reached!*\nFinal Balance: {stats['current_balance']:,}")
                        is_running = False
                        break
                
                # ----- BATCH DELAY -----
                time.sleep(random.uniform(0.5, 1.0))
            
            ws.close()
            logger.info("WebSocket closed")
            
        except Exception as e:
            logger.error(f"Farm loop error: {e}")
            with stats_lock:
                stats["last_error"] = str(e)
            time.sleep(3)
    
    logger.info("Farm loop ended")

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
def get_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    btn = "🛑 Stop" if is_running else "▶️ Start"
    markup.add(
        InlineKeyboardButton(btn, callback_data="toggle"),
        InlineKeyboardButton("🔑 Token", callback_data="set_token")
    )
    markup.add(
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("🎯 Target", callback_data="set_target")
    )
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    global config
    if config["owner_id"] is None:
        config["owner_id"] = message.chat.id
        save_config()
    
    bot.send_message(
        message.chat.id,
        "💰 *FARM BOT (FIXED)*\n\n"
        "✅ Fixed: Burst 80, Package 3\n"
        "✅ Fixed: Timeout 1.5s\n"
        "✅ Fixed: Reconnect 3s\n\n"
        "Menu ကို သုံးပါ။",
        reply_markup=get_menu(),
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['target'])
def cmd_target(message):
    global config
    chat_id = message.chat.id
    if config["owner_id"] != chat_id:
        return
    
    try:
        args = message.text.split()
        if len(args) < 2:
            msg = bot.send_message(
                chat_id,
                "ℹ️ `/target <ပမာဏ>`\nဥပမာ: `/target 150000000`",
                parse_mode="Markdown"
            )
            delete_msg_after(chat_id, msg.message_id, 10)
            return
        
        new_target = int(args[1].replace(",", "").replace(".", ""))
        if new_target <= 0:
            msg = bot.send_message(chat_id, "❌ 0 ထက်ကြီးရမယ်")
            delete_msg_after(chat_id, msg.message_id, 5)
            return
        
        config["target"] = new_target
        save_config()
        msg = bot.send_message(
            chat_id,
            f"🎯 Target: {new_target:,}",
            parse_mode="Markdown"
        )
        delete_msg_after(chat_id, msg.message_id, 10)
        
    except:
        msg = bot.send_message(chat_id, "❌ Invalid number")
        delete_msg_after(chat_id, msg.message_id, 5)

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running, farm_thread, config
    chat_id = call.message.chat.id
    
    if config["owner_id"] != chat_id:
        bot.answer_callback_query(call.id, "❌ No permission")
        return
    
    if call.data == "toggle":
        if is_running:
            is_running = False
            bot.answer_callback_query(call.id, "🛑 Stopping...")
        else:
            if not config["token"]:
                bot.answer_callback_query(call.id, "❌ Set token first!", show_alert=True)
                return
            is_running = True
            farm_thread = threading.Thread(
                target=farm_loop,
                args=(config["token"], chat_id),
                daemon=True
            )
            farm_thread.start()
            bot.answer_callback_query(call.id, "▶️ Starting...")
        
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_menu())
    
    elif call.data == "set_token":
        msg = bot.send_message(chat_id, "🔑 Send token:")
        bot.register_next_step_handler(msg, process_token)
        bot.answer_callback_query(call.id)
    
    elif call.data == "set_target":
        msg = bot.send_message(
            chat_id,
            "🎯 Target ကို ရိုက်ထည့်ပါ:\nဥပမာ: `150000000`",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, process_target)
        bot.answer_callback_query(call.id)
    
    elif call.data == "status":
        with stats_lock:
            status = "🟢 Running" if is_running else "🔴 Stopped"
            text = (
                f"📊 *Status*\n"
                f"State: {status}\n"
                f"🎯 Target: {config['target']:,}\n"
                f"💰 Balance: {stats['current_balance']:,}\n"
                f"📈 Gained: +{stats['total_gained']:,}\n"
                f"🔄 Claims: {stats['claims_count']}\n"
                f"⚡ Burst: {stats['current_burst']}\n"
                f"📦 Package: {stats['current_package']}\n"
                f"❌ Error: {stats['last_error']}"
            )
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        delete_msg_after(chat_id, msg.message_id, 15)
        bot.answer_callback_query(call.id)

def process_token(message):
    token = parse_token(message.text)
    chat_id = message.chat.id
    
    try:
        bot.delete_message(chat_id, message.message_id)
    except:
        pass
    
    if token:
        config["token"] = token
        save_config()
        msg = bot.send_message(chat_id, "✅ Token updated!")
        delete_msg_after(chat_id, msg.message_id, 3)
    else:
        msg = bot.send_message(chat_id, "❌ Invalid token")
        delete_msg_after(chat_id, msg.message_id, 3)
    
    bot.edit_message_reply_markup(chat_id, message.message_id - 1, reply_markup=get_menu())

def process_target(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except:
        pass
    
    try:
        target = int(message.text.replace(",", "").replace(".", ""))
        if target <= 0:
            raise ValueError()
        
        config["target"] = target
        save_config()
        msg = bot.send_message(
            chat_id,
            f"🎯 Target: {target:,}",
            parse_mode="Markdown"
        )
        delete_msg_after(chat_id, msg.message_id, 5)
    except:
        msg = bot.send_message(chat_id, "❌ Invalid")
        delete_msg_after(chat_id, msg.message_id, 3)
    
    bot.edit_message_reply_markup(chat_id, message.message_id - 1, reply_markup=get_menu())

if __name__ == "__main__":
    logger.info("🚀 FARM BOT (FIXED) STARTING")
    while True:
        try:
            bot.infinity_polling(timeout=60)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)
