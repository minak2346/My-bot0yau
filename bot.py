import websocket
import msgpack
import json
import time
import threading
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import ssl
import random
import os
import math
import sys
from urllib.parse import urlparse, parse_qs

# Force unbuffered output for GitHub Actions logs
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# CONFIGURATION
# ==========================================
# GitHub Secret မှ Token ကို လုံခြုံစွာ ခေါ်ယူခြင်း
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    print("[CRITICAL] TELEGRAM_BOT_TOKEN is missing! Please set it in your environment variables/secrets.", flush=True)
    sys.exit(1)

WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "bot_config.json"

WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn",
    "Accept-Language: my-MM,my;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With: com.mytel.myid"
]

print(f"[DEBUG] Initializing Telegram Bot...", flush=True)
try:
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    print("[DEBUG] Telegram Bot object created.", flush=True)
except Exception as e:
    print(f"[CRITICAL] Failed to initialize Telegram Bot: {e}", flush=True)
    sys.exit(1)

# ==========================================
# STATE
# ==========================================
config_data = {"owner_id": None, "game_access_token": None, "auto_restart": True, "speed_multiplier": 200}
is_running = False
login_only_mode = False  
ws_conn = None
ws_lock = threading.Lock()
game_creds = {"username": "", "password": ""}

# Message cleanup state
sent_messages = []
msg_lock = threading.Lock()
last_menu_message_id = None

heartbeat_alive = False
shoot_alive = False
use_4x_alive = False
in_game = False
login_handled = False
play_handled = False

# ==========================================
# LUCKY WHEEL (UNLIMITED EXCHANGE)
# ==========================================
GOLD_PER_SPIN = 20000
msg_id_counter = 9999  
msg_id_lock = threading.Lock()
pending_requests = {}  
request_lock = threading.Lock()

# Persistent lucky wheel state
lw_state = {"spins": None, "cost_per_spin": None, "gold": None}
lw_state_lock = threading.Lock()

# ==========================================
# STATISTICS TRACKING
# ==========================================
stats = {
    "requests_sent": 0,
    "coins_spent": 0,
    "coins_gained": 0,
    "fish_killed": 0,
    "start_balance": 0,
    "current_balance": 0
}
stats_lock = threading.Lock()

def reset_session_stats():
    global stats
    with stats_lock:
        stats["requests_sent"] = 0
        stats["coins_spent"] = 0
        stats["coins_gained"] = 0
        stats["fish_killed"] = 0

def log_stats():
    with stats_lock:
        profit = stats["coins_gained"] - stats["coins_spent"]
        print(f"\n--- SESSION STATISTICS ---", flush=True)
        print(f"Requests Sent: {stats['requests_sent']}", flush=True)
        print(f"Coins Spent: {stats['coins_spent']:,}", flush=True)
        print(f"Coins Gained: {stats['coins_gained']:,}", flush=True)
        print(f"Net Profit: {profit:,}", flush=True)
        print(f"Fish Killed: {stats['fish_killed']}", flush=True)
        print(f"Current Balance: {stats['current_balance']:,}", flush=True)
        print(f"--------------------------\n", flush=True)

# ==========================================
# CYCLE CONFIGURATION
# ==========================================
cycle_duration = 120
cycle_pause = 5

# ==========================================
# ERROR MONITORING
# ==========================================
error_count = 0
max_errors = 1 
last_error_msg = "None"
restart_lock = threading.Lock()
is_restarting = False

# ==========================================
# OPTIMIZED GAME LOGIC VARIABLES - ULTRA SPEED
# ==========================================
SPEED_MULTIPLIER = 200 
bullet_speed = 1400   
fish_list = {}        
fish_lock = threading.Lock()
last_server_time = 0  

current_angle_deg = 0.0
drag_direction = 1

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data = json.load(f)
        except: pass

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f)

load_config()

# ==========================================
# UTILS
# ==========================================
def parse_game_url(url_or_token):
    url_or_token = url_or_token.strip()
    if "fishmya" in url_or_token or url_or_token.startswith("http"):
        parsed = urlparse(url_or_token)
        params = parse_qs(parsed.query)
        token = params.get("access_token", [None])[0]
        if not token:
            return None
        return token
    else:
        return url_or_token if url_or_token.startswith("eyJ") else None

def next_msg_id():
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        return msg_id_counter

def send_ws(ws, payload_dict):
    global error_count, last_error_msg, stats
    if ws and ws.connected:
        try:
            ws.send(msgpack.packb(payload_dict, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            with stats_lock:
                stats["requests_sent"] += 1
                if payload_dict.get("route") == "shoot":
                    stats["coins_spent"] += 6
            return True
        except Exception as e:
            if not is_restarting:
                error_count += 1
                last_error_msg = f"Send error: {str(e)}"
            print(f"[SEND] Error: {e}")
    return False

# ==========================================
# TELEGRAM UI & AUTO-DELETE CLEANUP
# ==========================================
def send_and_auto_delete(chat_id, text, delay=10, markup=None, parse_mode="Markdown"):
    """စာများကို ပို့ပြီး (delay) စက္ကန့်အကြာတွင် အလိုအလျောက် ဖျက်ပေးသည့် လုပ်ဆောင်ချက်"""
    try:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode=parse_mode)
        def delete_task():
            time.sleep(delay)
            try:
                bot.delete_message(chat_id, msg.message_id)
            except:
                pass
        threading.Thread(target=delete_task, daemon=True).start()
        return msg
    except Exception as e:
        print(f"[UI] Error sending auto-delete message: {e}")
        return None

def get_main_menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("▶️ Start Bot", callback_data="cmd_start"),
        InlineKeyboardButton("🛑 Stop Bot", callback_data="cmd_stop"),
        InlineKeyboardButton("🔑 Set Token", callback_data="cmd_token"),
        InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
        InlineKeyboardButton("⚡ Set Speed", callback_data="cmd_speed"),
        InlineKeyboardButton("🔧 Force Restart", callback_data="cmd_force_restart"),
        InlineKeyboardButton("🎡 Lucky Wheel", callback_data="cmd_lucky_menu"),
        InlineKeyboardButton("💱 Exchange Spins", callback_data="cmd_buy_menu"),
        InlineKeyboardButton("🔐 Login Only", callback_data="cmd_login_only")
    )
    return markup

def get_buy_menu_markup(max_affordable):
    EXCHANGE_PRESETS = [
        (99999, "99,999"), (500000, "500K"), (1000000, "1M"),
        (5000000, "5M"), (10000000, "10M"), (9999999, "MAX🔥"),
    ]
    markup = InlineKeyboardMarkup(row_width=3)
    for amount, label in EXCHANGE_PRESETS:
        if label == "MAX🔥":
            markup.add(InlineKeyboardButton(f"💱 MAX ({max_affordable:,})", callback_data=f"buy_{max_affordable}"))
        else:
            markup.add(InlineKeyboardButton(f"💱 {label}", callback_data=f"buy_{amount}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="cmd_lucky_menu"))
    return markup

def get_lucky_menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🎰 Lucky Balance", callback_data="cmd_lucky_balance"),
        InlineKeyboardButton("💱 Exchange", callback_data="cmd_buy_menu"),
        InlineKeyboardButton("🔙 Main Menu", callback_data="cmd_back")
    )
    return markup

def do_exchange(ws, amount, chat_id):
    mid = next_msg_id()
    with request_lock:
        pending_requests[mid] = {"route": "buyLuckySpin", "amount": amount, "time": time.time()}
    with ws_lock:
        if not (ws and ws.connected):
            return "❌ Game connection မရှိ — ဦးစွာ Start Bot နှိပ့်ပါ"
        if not send_ws(ws, {"route": "buyLuckySpin", "data": {"count": amount}, "msgId": mid}):
            return "❌ Send failed"
    return f"⏳ လုက်ခွင့့် **{amount:,}** လဲလှယ့်မှု ပို့ပြီးပါပြီ — တုံ့ပြန်မှု စောင့်နေပါတယ်..."

def do_get_lucky_spin(ws):
    mid = next_msg_id()
    with request_lock:
        pending_requests[mid] = {"route": "getLuckySpin", "time": time.time()}
    send_ws(ws, {"route": "getLuckySpin", "data": {}, "msgId": mid})

def clean_and_send_menu(chat_id, text=None, markup=None):
    global last_menu_message_id
    with msg_lock:
        if last_menu_message_id:
            try: bot.delete_message(chat_id, last_menu_message_id)
            except: pass

    if not text:
        text = "🤖 *Fish Bot HYPER SPEED (2X)*\n⚡ Shoot: Held Down\n🐟 Targets: 2 fish\n🔫 Bullet Speed: 1400\n\nSelect action:"
    try:
        markup = markup if markup is not None else get_main_menu_markup()
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        last_menu_message_id = msg.message_id
    except Exception as e:
        print(f"[UI] Error sending menu: {e}")

def track_and_send(chat_id, text, markup=None, auto_delete=True, delay=15):
    """ဆာဗာမှ ပြန်လာသည့် အချက်အလက်များကို ပို့ပေးပြီး အချိန်ခဏအကြာတွင် ဖျက်ပေးမည်"""
    try:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        if auto_delete:
            def delete_task():
                time.sleep(delay)
                try: bot.delete_message(chat_id, msg.message_id)
                except: pass
            threading.Thread(target=delete_task, daemon=True).start()
        return msg
    except Exception as e:
        print(f"[UI] Error sending track message: {e}")
        return None

def stop_all_threads():
    global heartbeat_alive, shoot_alive, use_4x_alive, login_handled, play_handled, in_game
    heartbeat_alive = False
    shoot_alive = False
    use_4x_alive = False
    login_handled = False
    play_handled = False
    in_game = False
    with fish_lock:
        fish_list.clear()

def force_restart():
    global is_restarting, error_count
    with restart_lock:
        if is_restarting: return
        is_restarting = True
    print("[RESTART] Executing restart...")
    error_count = 0
    with ws_lock:
        if ws_conn:
            try: ws_conn.close()
            except: pass
    stop_all_threads()
    time.sleep(1)
    is_restarting = False

# ==========================================
# CORE MANAGER
# ==========================================
def bot_manager_loop():
    global is_running, ws_conn, error_count, last_error_msg, is_restarting
    print("[MANAGER] Started.")
    while True:
        if not is_running:
            time.sleep(1)
            continue
        print("[MANAGER] Attempting connection...")
        start_ws_connection()
        start_time = time.time()
        while is_running and not is_restarting:
            elapsed = time.time() - start_time
            if elapsed >= cycle_duration:
                print(f"[MANAGER] Cycle finished ({cycle_duration}s). Pausing...")
                log_stats()
                break
            if error_count >= max_errors:
                print(f"[MANAGER] Max errors reached ({error_count}). Restarting...")
                log_stats()
                if config_data["owner_id"]:
                    send_and_auto_delete(config_data["owner_id"], f"🔧 *Auto Restart*\n⚠️ Error: {last_error_msg}\n🔄 Restarting now...")
                break
            if in_game and not shoot_alive and ws_conn and ws_conn.connected:
                print("[MANAGER] Shoot thread died. Restarting...")
                log_stats()
                break
            time.sleep(1)
        print("[MANAGER] Closing connection for next phase...")
        with restart_lock: is_restarting = True
        with ws_lock:
            if ws_conn:
                try: ws_conn.close()
                except: pass
            ws_conn = None
        stop_all_threads()
        error_count = 0
        if is_running:
            print(f"[MANAGER] Pausing for {cycle_pause}s...")
            time.sleep(cycle_pause)
        with restart_lock: is_restarting = False

def start_ws_connection():
    global ws_conn
    token = config_data.get("game_access_token")
    if not token: return
    url = f"{WS_URL}?access_token={token}"
    try:
        conn = websocket.create_connection(url, header=WS_HEADERS, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=30)
        with ws_lock: ws_conn = conn
        print("[WS] Connected. Sending login...")
        send_ws(conn, {"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1})
        threading.Thread(target=ws_recv_loop, args=(conn,), daemon=True).start()
    except Exception as e:
        print(f"[WS] Connection failed: {e}")
        time.sleep(2)

def ws_recv_loop(ws):
    while ws.connected and not is_restarting:
        try:
            data = ws.recv()
            if not data: break
            handle_message(data, ws)
        except: break

# ==========================================
# GAME LOOPS
# ==========================================
def heartbeat_loop(ws):
    global heartbeat_alive
    heartbeat_alive = True
    while is_running and heartbeat_alive and ws.connected and not is_restarting:
        send_ws(ws, {"route": "ping", "data": {}, "msgId": 0})
        time.sleep(2)
    heartbeat_alive = False

def auto_shoot_loop(ws):
    global shoot_alive, current_angle_deg, drag_direction
    shoot_alive = True
    while is_running and shoot_alive and ws.connected and not is_restarting:
        try:
            target_ids = []
            with fish_lock:
                current_fish_ids = list(fish_list.keys()) 
                if current_fish_ids: target_ids = current_fish_ids[:2]
            
            current_angle_deg += drag_direction * 0.05
            if current_angle_deg >= 60.0:
                current_angle_deg = 60.0
                drag_direction = -1
            elif current_angle_deg <= -60.0:
                current_angle_deg = -60.0
                drag_direction = 1
            
            angle_rad = math.radians(current_angle_deg)
            multiplier = int(config_data.get("speed_multiplier", 200))
            batch_size = 10
            num_batches = max(1, multiplier // batch_size)
            
            for _ in range(num_batches):
                if not (ws.connected and is_running and not is_restarting): break
                for _ in range(batch_size):
                    send_ws(ws, {
                        "route": "shoot",
                        "data": {"rad": angle_rad, "type": 4, "target": target_ids[0] if target_ids else -1, "rapidFire": True, "auto": True, "bulletSpeed": bullet_speed},
                        "msgId": 0
                    })
                    if target_ids:
                        send_ws(ws, {
                            "route": "clientHitFish",
                            "data": {"btype": 4, "skillType": 0, "fIds": target_ids, "bulletSpeed": bullet_speed},
                            "msgId": 0
                        })
                time.sleep(0.005)
            time.sleep(0.01)
        except Exception as e:
            print(f"[GG-SPEED] Loop Error: {e}")
            break
    shoot_alive = False

def use_4x_loop(ws):
    global use_4x_alive
    use_4x_alive = True
    while is_running and use_4x_alive and ws.connected and not is_restarting:
        send_ws(ws, {"route": "useItem", "data": {"type": 6}, "msgId": 0})
        time.sleep(10)
    use_4x_alive = False

# ==========================================
# MESSAGE HANDLER
# ==========================================
def handle_message(data, ws):
    global game_creds, login_handled, play_handled, fish_list, last_server_time, error_count, last_error_msg, stats
    try:
        decoded = msgpack.unpackb(data, raw=False)
        if not isinstance(decoded, dict): return
        route = decoded.get("route", "")
        msg_id = decoded.get("msgId", -1)
        inner = decoded.get("data", decoded)
        if not isinstance(inner, dict): inner = {}
        
        if route == "OnUpdateObjects":
            objects = inner.get("objects", [])
            dead_fish = inner.get("deadFish", [])
            with fish_lock:
                for obj in objects:
                    f_id = obj.get("id")
                    if f_id: fish_list[f_id] = obj
                for df in dead_fish:
                    f_id = df.get("id")
                    if f_id in fish_list: del fish_list[f_id]
        elif route == "OnUpdateObject":
            f_id = inner.get("id")
            if f_id:
                with fish_lock: fish_list[f_id] = inner
        elif route == "OnObjectDie":
            f_id = inner.get("id")
            with fish_lock:
                if f_id in fish_list: del fish_list[f_id]
            if inner.get("playerId") == game_creds.get("username"):
                with stats_lock:
                    stats["fish_killed"] += 1
                    stats["coins_gained"] += inner.get("cash", 0)
        elif route == "OnUpdateCash":
            if inner.get("playerId") == game_creds.get("username"):
                with stats_lock:
                    stats["current_balance"] = inner.get("cash", 0)
                with lw_state_lock:
                    lw_state["gold"] = inner.get("cash", 0)

        with request_lock:
            pending = pending_requests.get(msg_id)
        if pending and msg_id >= 10000:
            with request_lock:
                pending_requests.pop(msg_id, None)
            if pending["route"] == "buyLuckySpin":
                if inner.get("code") == 0:
                    with lw_state_lock:
                        lw_state["gold"] = inner.get("cash", lw_state.get("gold"))
                        lw_state["spins"] = None
                    if config_data["owner_id"]:
                        track_and_send(
                            config_data["owner_id"],
                            f"✅ *လဲလှယ်မှု အောင်မြင်ပါပြီ!*\n\n"
                            f"🎰 လဲလှယ်ပြီး: **{pending.get('amount', '?'):,}** လုက်ခွင့်\n"
                            f"🪙 Gold အသစ်: **{inner.get('cash', 0):,}**\n\n"
                            f"⚠️ ရွှေ နုတ်ထုတ်သွားမှု: **{pending.get('amount', 0) * GOLD_PER_SPIN:,}**",
                            get_buy_menu_markup(inner.get("cash", 0) // GOLD_PER_SPIN),
                            auto_delete=True, delay=20
                        )
                else:
                    if config_data["owner_id"]:
                        send_and_auto_delete(config_data["owner_id"], f"❌ *လဲလှယ်မှု ကျရှုံး*\nCode: {inner.get('code')}\nMsg: {inner.get('msg', '—')}")
            elif pending["route"] == "getLuckySpin":
                spin = inner.get("spin")
                cost = inner.get("cost")
                with lw_state_lock:
                    lw_state["spins"] = spin
                    lw_state["cost_per_spin"] = cost
                if config_data["owner_id"]:
                    gold = lw_state.get("gold") or 0
                    max_buy = gold // GOLD_PER_SPIN if gold else 0
                    track_and_send(
                        config_data["owner_id"],
                        f"🎡 *Lucky Wheel Status*\n\n"
                        f"🎰 လုက်ခွင့်: **{spin:,}**\n"
                        f"🔁 တစ်ခြုကျ: **{cost:,}** gold\n"
                        f"🪙 Gold: **{gold:,}**\n"
                        f"💱 အများဆုံး လဲလို့ရ: **{max_buy:,}**",
                        get_buy_menu_markup(max_buy),
                        auto_delete=True, delay=20
                    )

        if msg_id == 1: 
            if inner.get("ok"):
                login_handled = True
                game_creds["username"] = inner.get("username", "")
                game_creds["password"] = inner.get("password", "")
                with stats_lock:
                    stats["start_balance"] = inner.get("cash", 0)
                    stats["current_balance"] = inner.get("cash", 0)
                with lw_state_lock:
                    lw_state["gold"] = inner.get("cash", 0)
                if config_data["owner_id"]:
                    if login_only_mode:
                        send_and_auto_delete(
                            config_data["owner_id"],
                            f"✅ *Login OK (Login Only Mode)*\n👤 {inner.get('nickname', 'User')}\n💰 Gold: **{inner.get('cash', 0):,}**",
                            delay=10
                        )
                    else:
                        send_and_auto_delete(config_data["owner_id"], f"✅ *Login OK!*\n👤 {inner.get('nickname', 'User')}\n💰 Balance: {inner.get('cash', 0):,}\n\n⚡ Entering room...", delay=10)
                if not heartbeat_alive: threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
                time.sleep(0.5)
                if not login_only_mode:
                    send_ws(ws, {"route": "play", "data": {"playerId": game_creds["username"], "password": game_creds["password"], "index": 0}, "msgId": 2})
                if login_only_mode:
                    do_get_lucky_spin(ws)
        elif msg_id == 2: 
            if inner.get("ok"):
                play_handled = True
                start_game_actions(ws)
    except Exception as e:
        if not is_restarting:
            error_count += 1
            last_error_msg = f"Handler error: {str(e)}"

def start_game_actions(ws):
    global in_game
    if not is_running or is_restarting: return
    in_game = True
    send_ws(ws, {"route": "useItem", "data": {"type": 4}, "msgId": 0})
    send_ws(ws, {"route": "clientActiveGun", "data": {"btype": 4, "gun": "gun1", "skillType": "none", "locationX": 0, "locationY": 0, "bulletSpeed": bullet_speed}, "msgId": 0})
    if not shoot_alive: threading.Thread(target=auto_shoot_loop, args=(ws,), daemon=True).start()
    if not use_4x_alive: threading.Thread(target=use_4x_loop, args=(ws,), daemon=True).start()

# ==========================================
# TELEGRAM COMMANDS
# ==========================================
@bot.message_handler(commands=['start'])
def handle_start_cmd(message):
    global config_data
    user_id = message.chat.id
    if config_data["owner_id"] is None:
        config_data["owner_id"] = user_id
        save_config()
        send_and_auto_delete(user_id, "👑 You are now the Owner!")
    elif config_data["owner_id"] != user_id: return
    clean_and_send_menu(user_id)

@bot.message_handler(commands=['speed'])
def handle_speed_cmd(message):
    global config_data
    user_id = message.chat.id
    if config_data["owner_id"] != user_id: return
    try:
        args = message.text.split()
        if len(args) < 2:
            send_and_auto_delete(user_id, "ℹ️ Usage: `/speed <value>`\nExample: `/speed 500`")
            return
        new_speed = int(args[1])
        if new_speed < 1:
            send_and_auto_delete(user_id, "❌ Speed must be at least 1.")
            return
        config_data["speed_multiplier"] = new_speed
        save_config()
        send_and_auto_delete(user_id, f"⚡ *Speed updated to {new_speed}x!*")
    except ValueError:
        send_and_auto_delete(user_id, "❌ Invalid number. Please enter an integer.")

@bot.message_handler(commands=['buy', 'exchange'])
def handle_buy_cmd(message):
    user_id = message.chat.id
    if config_data["owner_id"] != user_id: return
    args = message.text.split()
    if len(args) < 2:
        send_and_auto_delete(user_id, "ℹ️ Usage: `/buy <amount>`\nExample: `/buy 1000000`\n`/buy max` — Gold အကုန်နဲ့ အများဆုံးလဲ")
        return
    amount_raw = args[1].strip().lower().replace(",", "").replace(".", "")
    if amount_raw == "max":
        with lw_state_lock:
            gold = lw_state.get("gold") or stats.get("current_balance", 0)
        amount = gold // GOLD_PER_SPIN if gold else 0
        if amount <= 0:
            send_and_auto_delete(user_id, "❌ Gold မရှိ — token စစ်ပါ")
            return
    else:
        try:
            amount = int(amount_raw)
        except ValueError:
            send_and_auto_delete(user_id, "❌ Invalid number.")
            return
    if amount <= 0:
        send_and_auto_delete(user_id, "❌ Amount must be > 0")
        return
    with ws_lock:
        conn = ws_conn
    if not (conn and conn.connected):
        send_and_auto_delete(user_id, "❌ Game connection မရှိ — ဦးစွာ Start Bot နှိပ့်ပါ")
        return
    result = do_exchange(conn, amount, user_id)
    send_and_auto_delete(user_id, result)

@bot.message_handler(commands=['lucky'])
def handle_lucky_cmd(message):
    user_id = message.chat.id
    if config_data["owner_id"] != user_id: return
    with ws_lock:
        conn = ws_conn
    if not (conn and conn.connected):
        send_and_auto_delete(user_id, "❌ Game connection မရှိ — ဦးစွာ Start Bot နှိပ့်ပါ")
        return
    send_and_auto_delete(user_id, "⏳ လုက်ခွင့့် ဘလန်စ် စစ်နေပါတယ်...", delay=5)
    do_get_lucky_spin(conn)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global is_running, config_data, login_only_mode
    try:
        user_id = call.message.chat.id
        if config_data["owner_id"] != user_id: return
        cmd = call.data
        if cmd == "cmd_start":
            if is_running: bot.answer_callback_query(call.id, "⚠️ Already running.")
            elif not config_data.get("game_access_token"): bot.answer_callback_query(call.id, "❌ Set token first!")
            else:
                is_running = True
                reset_session_stats()
                bot.answer_callback_query(call.id, "⚡ Starting...")
        elif cmd == "cmd_stop":
            is_running = False
            login_only_mode = False
            bot.answer_callback_query(call.id, "🛑 Stopping...")
            clean_and_send_menu(user_id, "🔴 Bot Stopped.")
        elif cmd == "cmd_force_restart":
            bot.answer_callback_query(call.id, "🔄 Restarting...")
            force_restart()
        elif cmd == "cmd_login_only":
            bot.answer_callback_query(call.id, "🔐 Login Only mode နှိပ့်ပီး...")
            if is_running:
                send_and_auto_delete(user_id, "⚠️ *Login Only* မသုံးမီ *Stop Bot* နှိပ့်ပေးပါ\n(လက်ရှိ ငါးပစ့် mode ဖြစ့်နေလို့)")
                return
            login_only_mode = True
            is_running = True
            with stats_lock:
                stats["coins_gained"] = 0
                stats["coins_spent"] = 0
                stats["fish_killed"] = 0
            clean_and_send_menu(user_id, "🔐 *Login Only Mode* စနေပါပြီ\n\n💡 Login ပဲ ၀င်ပါမယ် — room မ၀င်ဘဲ ငါးလည်း မပစ်ပါ။\n🎡 Lucky Wheel လဲလှယ်ခွင့် သီးသန့် သုံးနိုင်ပါပြီ။")
        elif cmd == "cmd_token":
            msg = bot.send_message(user_id, "🔑 *Send your Game URL or Access Token:*", parse_mode="Markdown")
            bot.register_next_step_handler(msg, handle_token_input)
            bot.answer_callback_query(call.id)
        elif cmd == "cmd_speed":
            current_speed = config_data.get("speed_multiplier", 200)
            msg = bot.send_message(user_id, f"⚡ *Current Speed: {current_speed}x*\n\nSend new speed value (e.g., 500, 1000):", parse_mode="Markdown")
            bot.register_next_step_handler(msg, handle_speed_input)
            bot.answer_callback_query(call.id)
        elif cmd == "cmd_status":
            status = "🟢 Running" if is_running else "🔴 Stopped"
            shoot = "🔥 Active" if shoot_alive else "💤 Idle"
            multiplier = config_data.get("speed_multiplier", 200)
            with stats_lock:
                profit = stats["coins_gained"] - stats["coins_spent"]
                stat_text = (
                    f"📊 *Bot Status*\n"
                    f"Status: {status}\n"
                    f"Shooting: {shoot}\n"
                    f"Speed: {multiplier}x\n"
                    f"--------------------------\n"
                    f"📡 Requests: {stats['requests_sent']}\n"
                    f"💸 Spent: {stats['coins_spent']:,}\n"
                    f"💰 Gained: {stats['coins_gained']:,}\n"
                    f"📈 Profit: {profit:,}\n"
                    f"🐟 Kills: {stats['fish_killed']}\n"
                    f"🏦 Balance: {stats['current_balance']:,}\n"
                    f"--------------------------\n"
                    f"Cycle: {cycle_duration}s run / {cycle_pause}s pause"
                )
            track_and_send(user_id, stat_text, auto_delete=True, delay=20)
            bot.answer_callback_query(call.id)
        elif cmd == "cmd_lucky_menu":
            bot.answer_callback_query(call.id)
            with ws_lock:
                conn = ws_conn
            if not (conn and conn.connected):
                send_and_auto_delete(user_id, "⚠️ Game connection မရှိ — ဦးစွာ *Start Bot* နှိပ့်ပါ", delay=7)
            else:
                with lw_state_lock:
                    spin = lw_state.get("spins")
                    gold = lw_state.get("gold") or stats.get("current_balance", 0)
                text = ("🎡 *Lucky Wheel*\n\n" +
                        (f"🎰 လုက်ခွင့့်: **{spin:,}**\n" if spin is not None else "🎰 လုက်ခွင့့်: သိရှိရန် *Lucky Balance* နှိပ့်ပါ\n") +
                        f"🪙 Gold: **{gold:,}**\n\n" +
                        "လဲလှယ်မည့် ပမာဏရွေးပါ:")
                max_buy = gold // GOLD_PER_SPIN if gold else 0
                clean_and_send_menu(user_id, text, get_buy_menu_markup(max_buy))
        elif cmd == "cmd_lucky_balance":
            bot.answer_callback_query(call.id)
            with ws_lock:
                conn = ws_conn
            if conn and conn.connected:
                send_and_auto_delete(user_id, "⏳ စစ်နေပါတယ်...", delay=5)
                do_get_lucky_spin(conn)
            else:
                send_and_auto_delete(user_id, "❌ Game connection မရှိ", delay=7)
        elif cmd == "cmd_buy_menu":
            bot.answer_callback_query(call.id)
            with ws_lock:
                conn = ws_conn
            with lw_state_lock:
                gold = lw_state.get("gold") or stats.get("current_balance", 0)
            max_buy = gold // GOLD_PER_SPIN if gold else 0
            if conn and conn.connected:
                clean_and_send_menu(user_id, f"💱 *လဲလှယ်မည့် ပမာဏ ရွေးပါ*\n🪙 Gold: **{gold:,}**\nအများဆုံး: **{max_buy:,}**", get_buy_menu_markup(max_buy))
            else:
                send_and_auto_delete(user_id, "❌ Game connection မရှိ — ဦးစွာ *Start Bot* နှိပ့်ပါ", delay=7)
        elif cmd == "cmd_back":
            bot.answer_callback_query(call.id)
            clean_and_send_menu(user_id)
        elif cmd.startswith("buy_"):
            bot.answer_callback_query(call.id)
            try:
                amount = int(cmd.split("_", 1)[1])
            except ValueError:
                send_and_auto_delete(user_id, "❌ Invalid", delay=5)
                return
            if amount <= 0:
                send_and_auto_delete(user_id, "❌ Amount must be > 0", delay=5)
                return
            with ws_lock:
                conn = ws_conn
            if not (conn and conn.connected):
                send_and_auto_delete(user_id, "❌ Game connection မရှိ", delay=7)
                return
            result = do_exchange(conn, amount, user_id)
            send_and_auto_delete(user_id, result, delay=10)
    except Exception as e:
        err = str(e)
        if "query is too old" not in err and "query ID is invalid" not in err:
            print(f"[CB] callback error: {e}")

def handle_token_input(message):
    global config_data
    user_id = message.chat.id
    token = parse_game_url(message.text.strip())
    if not token:
        send_and_auto_delete(user_id, "❌ Invalid. Try again.")
        return
    config_data["game_access_token"] = token
    save_config()
    clean_and_send_menu(user_id, "✅ Token updated!")
    bot.delete_message(user_id, message.message_id)  

def handle_speed_input(message):
    global config_data
    user_id = message.chat.id
    try:
        new_speed = int(message.text.strip())
        if new_speed < 1:
            send_and_auto_delete(user_id, "❌ Speed must be at least 1.")
            return
        config_data["speed_multiplier"] = new_speed
        save_config()
        clean_and_send_menu(user_id, f"✅ Speed updated to {new_speed}x!")
        bot.delete_message(user_id, message.message_id) 
    except ValueError:
        send_and_auto_delete(user_id, "❌ Invalid number. Please enter an integer.")

if __name__ == "__main__":
    print("[STARTUP] Starting Bot Manager Thread...", flush=True)
    threading.Thread(target=bot_manager_loop, daemon=True).start()
    
    if config_data.get("game_access_token"):
        print("[STARTUP] Found access token, setting is_running = True", flush=True)
        is_running = True
    else:
        print("[STARTUP] No access token found. Bot will wait for command.", flush=True)
        
    print("[STARTUP] Starting Telegram Polling...", flush=True)
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10)
    except Exception as e:
        print(f"[CRITICAL] Telegram Polling crashed: {e}", flush=True)
        sys.exit(1)
