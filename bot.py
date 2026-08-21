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

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "bot_config.json"

WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn",
    "X-Requested-With: com.mytel.myid"
]

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ==========================================
# STATE
# ==========================================
config_data = {"owner_id": None, "game_access_token": None, "auto_restart": True, "room_index": 3, "cycle_duration": 300, "cycle_pause": 5}
is_running = False
ws_conn = None
ws_lock = threading.Lock()
game_creds = {"username": "", "password": "", "userId": 0}

stats = {"requests_sent": 0, "coins_gained": 0, "fish_killed": 0, "current_balance": 0, "snipes_sent": 0}
stats_lock = threading.Lock()
fish_list = {}
fish_lock = threading.Lock()

heartbeat_alive = False
exploit_alive = False

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data.update(json.load(f))
        except: pass

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f)

load_config()

def send_ws(ws, payload_dict):
    if ws and ws.connected:
        try:
            ws.send(msgpack.packb(payload_dict, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            with stats_lock: stats["requests_sent"] += 1
            return True
        except: pass
    return False

# ==========================================
# CORE MANAGER
# ==========================================
def bot_manager_loop():
    global is_running, ws_conn
    while True:
        if not is_running:
            time.sleep(1)
            continue
        
        token = config_data.get("game_access_token")
        if not token:
            time.sleep(1)
            continue

        print(f"[MANAGER] Starting {config_data['cycle_duration']}s cycle...", flush=True)
        try:
            url = f"{WS_URL}?access_token={token}"
            ws = websocket.create_connection(url, header=WS_HEADERS, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=10)
            with ws_lock: ws_conn = ws
            
            send_ws(ws, {"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1})
            
            start_time = time.time()
            while is_running and ws.connected:
                # Cycle check
                if time.time() - start_time > config_data["cycle_duration"]:
                    print("[MANAGER] Cycle complete. Restarting...", flush=True)
                    break
                
                try:
                    ws.settimeout(1.0)
                    data = ws.recv()
                    if not data: break
                    handle_message(data, ws)
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as e:
                    print(f"[MANAGER] Recv error: {e}", flush=True)
                    break
            
            ws.close()
        except Exception as e:
            print(f"[MANAGER] Connection error: {e}", flush=True)
        
        if is_running:
            print(f"[MANAGER] Pausing for {config_data['cycle_pause']}s...", flush=True)
            time.sleep(config_data["cycle_pause"])

def handle_message(data, ws):
    global game_creds, fish_list, stats, heartbeat_alive, exploit_alive
    try:
        d = msgpack.unpackb(data, raw=False)
        route = d.get("route")
        msg_id = d.get("msgId")
        inner = d.get("data", {})

        if route == "OnUpdateObject":
            objs = inner if isinstance(inner, list) else [inner]
            with fish_lock:
                for obj in objs:
                    fid = obj.get("id")
                    if fid:
                        fish_list[fid] = obj
                        if 0 < obj.get("h", 1.0) < 0.5:
                            send_ws(ws, {"route": "clientHitFish", "data": {"btype": 6, "skillType": 0, "fIds": [fid]}, "msgId": 100})
                            with stats_lock: stats["snipes_sent"] += 1
        
        elif route == "OnObjectDie":
            if inner.get("playerId") == game_creds.get("userId"):
                with stats_lock:
                    stats["fish_killed"] += 1
                    stats["coins_gained"] += inner.get("value", 0)
            with fish_lock:
                if inner.get("id") in fish_list: del fish_list[inner.get("id")]

        elif route == "reloadCash":
            with stats_lock: stats["current_balance"] = inner.get("newCash", 0)

        if msg_id == 1:
            if d.get("ok"):
                game_creds.update({"username": inner.get("username"), "password": inner.get("password"), "userId": inner.get("userId")})
                with stats_lock: stats["current_balance"] = inner.get("cash", 0)
                send_ws(ws, {"route": "play", "data": {"playerId": game_creds["username"], "password": game_creds["password"], "index": config_data["room_index"]}, "msgId": 2})
                if not heartbeat_alive: threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
                if not exploit_alive: threading.Thread(target=exploit_loop, args=(ws,), daemon=True).start()
    except: pass

def heartbeat_loop(ws):
    global heartbeat_alive
    heartbeat_alive = True
    while is_running and ws.connected:
        send_ws(ws, {"route": "heartBeat", "data": {}, "msgId": 999})
        time.sleep(15)
    heartbeat_alive = False

def exploit_loop(ws):
    global exploit_alive
    exploit_alive = True
    while is_running and ws.connected:
        send_ws(ws, {"route": "claimItemOnline", "data": {"package": 5}, "msgId": 500})
        time.sleep(1)
    exploit_alive = False

# ==========================================
# TELEGRAM
# ==========================================
def get_main_menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("▶️ Start Sniper", callback_data="cmd_start"),
        InlineKeyboardButton("🛑 Stop Bot", callback_data="cmd_stop"),
        InlineKeyboardButton("🔑 Set Token", callback_data="cmd_token"),
        InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
        InlineKeyboardButton("🔧 Force Restart", callback_data="cmd_force_restart")
    )
    return markup

@bot.message_handler(commands=['start'])
def handle_start(message):
    global config_data
    if config_data["owner_id"] is None:
        config_data["owner_id"] = message.chat.id
        save_config()
    bot.send_message(message.chat.id, "🤖 *Fish Sniper V4*\nCycle: 5 mins | Room: 3\nExploit: Active", reply_markup=get_main_menu_markup(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global is_running
    if call.data == "cmd_start":
        is_running = True
        bot.answer_callback_query(call.id, "🚀 Started!")
    elif call.data == "cmd_stop":
        is_running = False
        bot.answer_callback_query(call.id, "🛑 Stopped.")
    elif call.data == "cmd_token":
        msg = bot.send_message(call.message.chat.id, "🔑 *Send Access Token:*", parse_mode="Markdown")
        bot.register_next_step_handler(msg, update_token)
    elif call.data == "cmd_status":
        with stats_lock:
            text = (f"📊 *Bot Status*\n"
                    f"Room: {config_data['room_index']} | Cycle: 5m\n"
                    f"💰 Balance: {stats['current_balance']:,}\n"
                    f"🐟 Kills: {stats['fish_killed']}\n"
                    f"🎯 Snipes: {stats['snipes_sent']}\n"
                    f"📈 Gain: {stats['coins_gained']:,}")
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown")

def update_token(message):
    token = message.text.strip()
    if "access_token=" in token:
        token = parse_qs(urlparse(token).query).get("access_token", [token])[0]
    config_data["game_access_token"] = token
    save_config()
    bot.send_message(message.chat.id, "✅ Token Updated!")

if __name__ == "__main__":
    threading.Thread(target=bot_manager_loop, daemon=True).start()
    bot.infinity_polling()
