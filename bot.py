import telebot
import websocket
import msgpack
import ssl
import time
import threading
import json
import sys
import os

# Force unbuffered output for GitHub Actions
sys.stdout.reconfigure(line_buffering=True)

# Config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Global State
user_state = {
    "token": "",
    "running": False,
    "kills": 0,
    "gold_gained": 0,
    "claims": 0,
    "last_balance": 0,
    "login_status": "Not Logged In"
}

WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn"
]

def update_status(chat_id, message_id=None):
    status_text = (
        f"🎮 *FISH HUNTER SNIPER BOT*\n\n"
        f"🔐 *Login Status:* {user_state['login_status']}\n"
        f"💰 *Current Gold:* {user_state['last_balance']:,}\n"
        f"🎯 *Fish Sniped:* {user_state['kills']}\n"
        f"🎁 *Gold Claims:* {user_state['claims']}\n"
        f"📈 *Total Gained:* {user_state['gold_gained']:,}\n\n"
        f"🔄 *Status:* {'🟢 Running' if user_state['running'] else '🔴 Stopped'}\n"
        f"📍 *Room:* Room 3 (Max Gold)\n"
        f"🚀 *Bullet:* Bullet 6 (500,000 Gold)"
    )
    
    try:
        if message_id:
            bot.edit_message_text(status_text, chat_id, message_id, parse_mode="Markdown")
        else:
            msg = bot.send_message(chat_id, status_text, parse_mode="Markdown")
            return msg.message_id
    except:
        pass

def sniper_thread(chat_id, msg_id):
    while user_state["running"]:
        try:
            user_state["login_status"] = "⏳ Connecting..."
            update_status(chat_id, msg_id)
            
            ws = websocket.create_connection(WS_URL + "?access_token=" + user_state["token"], header=HEADERS, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=20)
            ws.send(msgpack.packb({"route": "mytelLogin", "data": {"accessToken": user_state["token"], "language": "my"}, "msgId": 1}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            
            user_id = 0
            username = ""
            password = ""
            
            # Heartbeat
            def hb():
                while ws.connected and user_state["running"]:
                    try:
                        ws.send(msgpack.packb({"route": "heartBeat", "data": {}, "msgId": 999}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                        time.sleep(15)
                    except: break
            threading.Thread(target=hb, daemon=True).start()
            
            # 5-minute cycle
            cycle_start = time.time()
            while user_state["running"] and time.time() - cycle_start < 300:
                try:
                    data = ws.recv()
                    if not data: break
                    
                    if isinstance(data, str): d = json.loads(data)
                    else: d = msgpack.unpackb(data, raw=False)
                    
                    route = d.get("route")
                    inner = d.get("data", {})
                    
                    if d.get("msgId") == 1:
                        if d.get("ok"):
                            user_state["login_status"] = "✅ Login Successful"
                            user_state["last_balance"] = inner.get("cash", 0)
                            username = inner.get("username")
                            password = inner.get("password")
                            user_id = inner.get("userId")
                            update_status(chat_id, msg_id)
                            
                            # Enter Room 3
                            ws.send(msgpack.packb({"route": "play", "data": {"playerId": username, "password": password, "index": 3}, "msgId": 2}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                            # Exploit Claim
                            ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": 3}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                        else:
                            user_state["login_status"] = "❌ Login Failed"
                            update_status(chat_id, msg_id)
                            user_state["running"] = False
                            break
                            
                    elif route == "OnUpdateObject":
                        objs = inner if isinstance(inner, list) else [inner]
                        for obj in objs:
                            if obj.get("h", 1.0) < 0.8: # Sniper target
                                ws.send(msgpack.packb({"route": "clientHitFish", "data": {"btype": 6, "skillType": 0, "fIds": [obj.get("id")]}, "msgId": 100}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    
                    elif route == "OnObjectDie":
                        if inner.get("playerId") == user_id:
                            user_state["kills"] += 1
                            user_state["gold_gained"] += inner.get("value", 0)
                            if user_state["kills"] % 5 == 0: update_status(chat_id, msg_id)
                            
                    elif route == "reloadCash":
                        user_state["claims"] += 1
                        user_state["last_balance"] = inner.get("newCash", user_state["last_balance"])
                        if user_state["claims"] % 10 == 0: update_status(chat_id, msg_id)
                        # Keep claiming
                        ws.send(msgpack.packb({"route": "claimItemOnline", "data": {"package": 5}, "msgId": 4}, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)

                except: continue
            
            ws.close()
            time.sleep(5) # Cooldown between cycles
        except Exception as e:
            user_state["login_status"] = f"⚠️ Error: {str(e)[:20]}"
            update_status(chat_id, msg_id)
            time.sleep(10)

@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Set Token", "Start Sniper", "Stop Bot")
    bot.send_message(message.chat.id, "Welcome to Fish Hunter Sniper Bot! Use the buttons below to control.", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "Set Token")
def set_token_prompt(message):
    bot.send_message(message.chat.id, "Please send your game token or URL.")
    bot.register_next_step_handler(message, save_token)

def save_token(message):
    token = message.text
    if "access_token=" in token:
        token = token.split("access_token=")[1].split("&")[0]
    user_state["token"] = token
    bot.send_message(message.chat.id, "✅ Token Saved!")

@bot.message_handler(func=lambda m: m.text == "Start Sniper")
def start_bot(message):
    if not user_state["token"]:
        bot.send_message(message.chat.id, "❌ Please set token first!")
        return
    if user_state["running"]:
        bot.send_message(message.chat.id, "🟡 Bot is already running.")
        return
    
    user_state["running"] = True
    msg_id = update_status(message.chat.id)
    threading.Thread(target=sniper_thread, args=(message.chat.id, msg_id), daemon=True).start()

@bot.message_handler(func=lambda m: m.text == "Stop Bot")
def stop_bot(message):
    user_state["running"] = False
    bot.send_message(message.chat.id, "🛑 Bot Stopping...")

if __name__ == "__main__":
    print("Bot is starting...")
    bot.polling(none_stop=True)
