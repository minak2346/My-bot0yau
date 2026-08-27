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

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# LOGGING SETUP
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('farm_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8875811759:AAEC_VPIoThZh_yYrkbnzgKBTTQv17roqs4"
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "farm_config_hybrid.json"

WS_HEADERS = {
    "User-Agent": "Android SM-S918B",
    "Origin": "https://fishmya.ugame.vn",
    "X-Requested-With": "com.mytel.myid"
}

# ==========================================
# BOT INITIALIZATION
# ==========================================
if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN is missing!")
    sys.exit(1)

try:
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    telebot.apihelper.CONNECT_TIMEOUT = 60
    telebot.apihelper.READ_TIMEOUT = 60
except Exception as e:
    logger.critical(f"Failed to initialize bot: {e}")
    sys.exit(1)

# ==========================================
# STATE
# ==========================================
config = {"owner_id": None, "token": None, "target": 150000000}
is_running = False
ws_conn = None
farm_thread = None
last_update_msg_id = None

stats = {
    "total_gained": 0,
    "claims_count": 0,
    "current_balance": 0,
    "start_balance": 0,
    "last_error": "None",
    "success_rate": 0.0,
    "current_burst": 150,
    "current_package": 5
}
stats_lock = threading.Lock()

# ==========================================
# FILE OPERATIONS
# ==========================================
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config.update(json.load(f))
            logger.info("Config loaded successfully")
        except Exception as e:
            logger.error(f"Error loading config: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        logger.info("Config saved successfully")
    except Exception as e:
        logger.error(f"Error saving config: {e}")

load_config()

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

def send_update(chat_id, text, auto_delete=False):
    global last_update_msg_id
    try:
        if auto_delete and last_update_msg_id:
            try:
                bot.delete_message(chat_id, last_update_msg_id)
            except:
                pass
        
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        if auto_delete:
            last_update_msg_id = msg.message_id
        return msg.message_id
    except Exception as e:
        logger.error(f"Failed to send update: {e}")
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
# HYBRID RATE LIMITER
# ==========================================
class HybridRateLimiter:
    def __init__(self):
        self.burst_size = 150  # မင်းရဲ့ Code ကနေ
        self.package_size = 5   # မင်းရဲ့ Code ကနေ
        self.min_burst = 50
        self.max_burst = 250
        self.min_package = 3
        self.max_package = 20
        self.success_count = 0
        self.fail_count = 0
        self.success_rate = 1.0
        self.adaptive_enabled = True
        
    def adjust(self):
        if not self.adaptive_enabled:
            return
        
        total = self.success_count + self.fail_count
        if total == 0:
            return
        
        self.success_rate = self.success_count / total
        
        # Adaptive Burst Size
        if self.success_rate > 0.8:
            self.burst_size = min(self.burst_size + 10, self.max_burst)
            self.package_size = min(self.package_size + 1, self.max_package)
            logger.info(f"📈 Increasing burst to {self.burst_size}, package to {self.package_size}")
        elif self.success_rate < 0.3:
            self.burst_size = max(self.burst_size - 20, self.min_burst)
            self.package_size = max(self.package_size - 1, self.min_package)
            logger.info(f"📉 Decreasing burst to {self.burst_size}, package to {self.package_size}")
        
        # Reset counters
        self.success_count = 0
        self.fail_count = 0
        
        # Update stats
        with stats_lock:
            stats["success_rate"] = self.success_rate
            stats["current_burst"] = self.burst_size
            stats["current_package"] = self.package_size

rate_limiter = HybridRateLimiter()

# ==========================================
# CORE FARMING LOGIC (HYBRID)
# ==========================================
def farm_loop(token, chat_id):
    global is_running, ws_conn, stats
    
    logger.info(f"🚀 Starting farm loop for {chat_id}")
    
    while is_running:
        try:
            # ==============================================
            # STEP 1: WebSocket Connection
            # ==============================================
            ws = websocket.create_connection(
                WS_URL,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=WS_HEADERS,
                timeout=30
            )
            ws_conn = ws
            logger.info("✅ WebSocket connected")
            
            # ==============================================
            # STEP 2: Login
            # ==============================================
            ws.send(msgpack.packb({
                "route": "mytelLogin", 
                "data": {"accessToken": token, "language": "my"}, 
                "msgId": 1
            }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            login_data = None
            for _ in range(40):
                m = ws.recv()
                d = msgpack.unpackb(m, raw=False)
                if d.get("msgId") == 1:
                    login_data = d.get("data", {})
                    break
            
            if not login_data or not login_data.get("ok"):
                logger.warning("Login failed, reconnecting...")
                time.sleep(10)
                continue
            
            balance = login_data.get("cash", 0)
            with stats_lock:
                stats["start_balance"] = balance
                stats["current_balance"] = balance
                stats["total_gained"] = 0
                stats["claims_count"] = 0
            
            logger.info(f"💰 Starting balance: {balance:,}")
            
            # ==============================================
            # STEP 3: Join Room
            # ==============================================
            ws.send(msgpack.packb({
                "route": "play", 
                "data": {"roomId": 1}, 
                "msgId": 2
            }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(1)
            
            # ==============================================
            # STEP 4: Main Farming Loop (HYBRID)
            # ==============================================
            msg_id_counter = 100
            last_gold_time = time.time()
            batch_counter = 0
            
            while is_running:
                # ---------- BURST SEND (မင်းရဲ့ Code ပုံစံ) ----------
                current_burst = rate_limiter.burst_size
                current_package = rate_limiter.package_size
                
                for _ in range(current_burst):
                    # Random package (5-20) နဲ့ ပို့ပါ
                    package = random.randint(current_package - 2, current_package + 2)
                    package = max(rate_limiter.min_package, min(rate_limiter.max_package, package))
                    
                    ws.send(msgpack.packb({
                        "route": "claimItemOnline", 
                        "data": {"package": package}, 
                        "msgId": msg_id_counter
                    }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    msg_id_counter += 1
                    
                    # Small delay to avoid detection
                    time.sleep(0.001)
                
                # ---------- RESPONSE READING (ငါ့ရဲ့ Code ပုံစံ) ----------
                success_count = 0
                ws.settimeout(1.0)
                
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
                                    success_count += 1
                                    last_gold_time = time.time()
                        
                        elif d.get("data", {}).get("ok") == False:
                            inner = d.get("data", {})
                            with stats_lock:
                                stats["last_error"] = inner.get("msg", "Action Failed")
                                
                except websocket.WebSocketTimeoutException:
                    pass
                except Exception as e:
                    logger.error(f"Response reading error: {e}")
                
                # ---------- ADAPTIVE ADJUSTMENT (ငါ့ရဲ့ Code ပုံစံ) ----------
                rate_limiter.success_count += success_count
                rate_limiter.fail_count += current_burst - success_count
                
                # Every 5 batches, adjust rate limiter
                batch_counter += 1
                if batch_counter >= 5:
                    rate_limiter.adjust()
                    batch_counter = 0
                
                # ---------- STATUS CHECK ----------
                # Gold မတက်ရင် ပြန်စ
                if time.time() - last_gold_time > 15:
                    logger.warning("No gold for 15s, reconnecting...")
                    break
                
                # Target ပြည့်ပြီလား?
                with stats_lock:
                    if stats["current_balance"] >= config["target"]:
                        logger.info(f"🎉 Target reached! {stats['current_balance']:,}")
                        send_update(chat_id, f"🎉 *Target Reached!*\nFinal Balance: {stats['current_balance']:,}")
                        is_running = False
                        break
                
                # ---------- BATCH DELAY ----------
                time.sleep(random.uniform(0.5, 1.5))
            
            ws.close()
            logger.info("WebSocket closed")
            
        except Exception as e:
            logger.error(f"Farm loop error: {e}")
            with stats_lock:
                stats["last_error"] = str(e)
            time.sleep(5)
    
    logger.info("Farm loop ended")

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
def get_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    btn = "🛑 Stop Farm" if is_running else "▶️ Start Farm"
    markup.add(
        InlineKeyboardButton(btn, callback_data="toggle"),
        InlineKeyboardButton("🔑 Set Token", callback_data="set_token")
    )
    markup.add(
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("🎯 Set Target", callback_data="set_target")
    )
    markup.add(
        InlineKeyboardButton("⚙️ Adaptive", callback_data="adaptive"),
        InlineKeyboardButton("📈 Stats", callback_data="detailed_stats")
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
        "💰 *HYBRID FARM BOT V1.0*\n\n"
        "🚀 အကောင်းဆုံး Farm Bot\n"
        "✅ Rate Limit Bypass\n"
        "✅ Adaptive Burst\n"
        "✅ Real-time Stats\n\n"
        "အောက်က Menu ကို သုံးပါ။",
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
                "ℹ️ *အသုံးပြုနည်း:*\n`/target <ပမာဏ>`\nဥပမာ: `/target 300000000`",
                parse_mode="Markdown"
            )
            delete_msg_after(chat_id, msg.message_id, 10)
            return
        
        new_target = int(args[1].replace(",", "").replace(".", ""))
        if new_target <= 0:
            msg = bot.send_message(chat_id, "❌ ပမာဏသည် 0 ထက် ကြီးရပါမည်။")
            delete_msg_after(chat_id, msg.message_id, 5)
            return
        
        config["target"] = new_target
        save_config()
        msg = bot.send_message(
            chat_id,
            f"🎯 *Target ကို {new_target:,} သို့ ပြောင်းလဲသတ်မှတ်လိုက်ပါပြီ!*",
            parse_mode="Markdown"
        )
        delete_msg_after(chat_id, msg.message_id, 10)
        
        try:
            bot.delete_message(chat_id, message.message_id)
        except:
            pass
        
    except ValueError:
        msg = bot.send_message(chat_id, "❌ ဂဏန်းမှားယွင်းနေပါသည်။")
        delete_msg_after(chat_id, msg.message_id, 5)

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running, farm_thread, config
    chat_id = call.message.chat.id
    
    if config["owner_id"] != chat_id:
        bot.answer_callback_query(call.id, "❌ ခွင့်မပြုပါ")
        return
    
    if call.data == "toggle":
        if is_running:
            is_running = False
            bot.answer_callback_query(call.id, "🛑 Stopping farm...")
        else:
            if not config["token"]:
                bot.answer_callback_query(call.id, "❌ Token မရှိပါ! Set Token လုပ်ပါ", show_alert=True)
                return
            is_running = True
            farm_thread = threading.Thread(
                target=farm_loop,
                args=(config["token"], chat_id),
                daemon=True
            )
            farm_thread.start()
            bot.answer_callback_query(call.id, "▶️ Starting farm...")
        
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_menu())
    
    elif call.data == "set_token":
        msg = bot.send_message(chat_id, "🔑 Access Token ကို ပို့ပါ:")
        bot.register_next_step_handler(msg, process_token)
        bot.answer_callback_query(call.id)
    
    elif call.data == "set_target":
        msg = bot.send_message(
            chat_id,
            "🎯 *Target ပမာဏကို ရိုက်ထည့်ပါ*\nဥပမာ: `300000000`",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, process_target)
        bot.answer_callback_query(call.id)
    
    elif call.data == "status":
        with stats_lock:
            status = "🟢 Running" if is_running else "🔴 Stopped"
            text = (
                f"📊 *Farm Status*\n"
                f"State: {status}\n"
                f"🎯 Target: {config['target']:,}\n"
                f"💰 Balance: {stats['current_balance']:,}\n"
                f"📈 Gained: +{stats['total_gained']:,}\n"
                f"🔄 Claims: {stats['claims_count']}\n"
                f"📊 Success Rate: {stats['success_rate']:.2%}\n"
                f"⚡ Burst: {stats['current_burst']}\n"
                f"📦 Package: {stats['current_package']}\n"
                f"❌ Last Error: {stats['last_error']}"
            )
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        delete_msg_after(chat_id, msg.message_id, 15)
        bot.answer_callback_query(call.id)
    
    elif call.data == "adaptive":
        rate_limiter.adaptive_enabled = not rate_limiter.adaptive_enabled
        status = "✅ ON" if rate_limiter.adaptive_enabled else "❌ OFF"
        bot.answer_callback_query(call.id, f"Adaptive: {status}")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_menu())
    
    elif call.data == "detailed_stats":
        with stats_lock:
            text = (
                f"📈 *Detailed Statistics*\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"💰 Balance: {stats['current_balance']:,}\n"
                f"📈 Total Gained: +{stats['total_gained']:,}\n"
                f"🔄 Total Claims: {stats['claims_count']}\n"
                f"📊 Success Rate: {stats['success_rate']:.2%}\n"
                f"⚡ Current Burst: {stats['current_burst']}\n"
                f"📦 Package Size: {stats['current_package']}\n"
                f"🎯 Target: {config['target']:,}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"🔄 Progress: {min(100, (stats['current_balance']/config['target'])*100):.1f}%"
            )
        msg = bot.send_message(chat_id, text, parse_mode="Markdown")
        delete_msg_after(chat_id, msg.message_id, 20)
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
        msg = bot.send_message(chat_id, "✅ Token updated successfully!")
        delete_msg_after(chat_id, msg.message_id, 3)
    else:
        msg = bot.send_message(chat_id, "❌ Invalid token format!")
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
            raise ValueError("Target must be positive")
        
        config["target"] = target
        save_config()
        msg = bot.send_message(
            chat_id,
            f"🎯 *Target ကို {target:,} သို့ ပြောင်းလဲသတ်မှတ်လိုက်ပါပြီ!*",
            parse_mode="Markdown"
        )
        delete_msg_after(chat_id, msg.message_id, 5)
    except:
        msg = bot.send_message(chat_id, "❌ Invalid target!")
        delete_msg_after(chat_id, msg.message_id, 3)
    
    bot.edit_message_reply_markup(chat_id, message.message_id - 1, reply_markup=get_menu())

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    logger.info("="*50)
    logger.info("🚀 HYBRID FARM BOT V1.0 STARTING")
    logger.info("="*50)
    logger.info(f"Bot Token: {TELEGRAM_BOT_TOKEN[:10]}...")
    logger.info(f"WebSocket: {WS_URL}")
    logger.info(f"Config File: {CONFIG_FILE}")
    logger.info("="*50)
    
    while True:
        try:
            bot.infinity_polling(timeout=60)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)
