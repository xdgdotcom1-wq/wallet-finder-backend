import asyncio
import aiohttp
import time
import json
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from mnemonic import Mnemonic
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from colorama import Fore, init

# --- INIT ---
app = FastAPI()
init(autoreset=True)

# --- CONFIG ---
ENABLE_BTC = True
ENABLE_ETH = True
ENABLE_TRX = True

# --- API ENDPOINTS ---
ETH_RPC = "https://eth.llamarpc.com"
TRX_API = "https://api.trongrid.io/v1/accounts/{}"
BTC_API = "https://blockstream.info/api/address/{}"

# --- RATE LIMITER ---
class RateLimiter:
    def __init__(self, rate_limit: int):
        self.rate_limit = rate_limit
        self.tokens = rate_limit
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def wait_for_token(self):
        if self.rate_limit <= 0:
            while self.rate_limit <= 0: await asyncio.sleep(1)
            return
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            new_tokens = elapsed * self.rate_limit
            if new_tokens > 0:
                self.tokens = min(self.rate_limit, self.tokens + new_tokens)
                self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
            else:
                wait_time = (1 - self.tokens) / self.rate_limit
                await asyncio.sleep(wait_time)
                self.tokens = 0
                self.last_refill = time.monotonic()
    
    def set_rate(self, new_rate: int): self.rate_limit = new_rate

# --- USER SESSION ---
class UserSession:
    def __init__(self, user_id: str, speed: int = 50):
        self.user_id = user_id
        self.limiter = RateLimiter(speed)
        self.active = True
        self.attempts = 0
        self.found = 0
        self.start_time = time.time()
        self.tasks = []
        self.socket: WebSocket = None  # Store the live connection

    def stop(self):
        self.active = False
        for task in self.tasks: task.cancel()

active_sessions = {}
session_lock = asyncio.Lock()

# --- WORKER ---
async def check_balance(session, url, method="GET", json_data=None):
    try:
        if method == "POST":
            async with session.post(url, json=json_data, timeout=5) as response:
                if response.status == 200: return await response.json()
        else:
            async with session.get(url, timeout=5) as response:
                if response.status == 200: return await response.json()
    except: pass
    return None

def derive_wallets(phrase):
    try:
        seed_bytes = Bip39SeedGenerator(phrase).Generate()
        wallets = {}
        if ENABLE_BTC: wallets['btc'] = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        if ENABLE_ETH: wallets['eth'] = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        if ENABLE_TRX: wallets['trx'] = Bip44.FromSeed(seed_bytes, Bip44Coins.TRON).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        return wallets
    except: return None

async def user_worker(user_session: UserSession, session: aiohttp.ClientSession, mnemo: Mnemonic):
    while user_session.active:
        try:
            await user_session.limiter.wait_for_token()
            phrase = mnemo.generate(strength=128)
            wallets = derive_wallets(phrase)
            if not wallets: continue

            # Checks
            tasks = []
            if ENABLE_BTC: tasks.append(check_balance(session, BTC_API.format(wallets['btc'])))
            else: tasks.append(asyncio.sleep(0))
            if ENABLE_ETH:
                payload = {"jsonrpc":"2.0","method":"eth_getBalance","params":[wallets['eth'], "latest"],"id":1}
                tasks.append(check_balance(session, ETH_RPC, "POST", payload))
            else: tasks.append(asyncio.sleep(0))
            if ENABLE_TRX: tasks.append(check_balance(session, TRX_API.format(wallets['trx'])))
            else: tasks.append(asyncio.sleep(0))

            results = await asyncio.gather(*tasks)
            
            # Result Logic
            btc_bal = 0
            eth_bal = 0
            trx_bal = 0
            found = False

            if results[0]:
                btc_bal = (results[0].get('chain_stats', {}).get('funded_txo_sum', 0) - results[0].get('chain_stats', {}).get('spent_txo_sum', 0)) / 100000000
                if btc_bal > 0: found = True
            
            if results[1] and "result" in results[1]:
                eth_bal = int(results[1]["result"], 16) / 10**18
                if eth_bal > 0: found = True

            if results[2] and results[2].get('data'):
                trx_bal = results[2]['data'][0]['balance'] / 1000000
                if trx_bal > 0: found = True

            user_session.attempts += 1
            if found: user_session.found += 1

            # Prepare data package for HTML
            data_packet = {
                "seedPhrase": phrase,
                "btcAddress": wallets['btc'], "btcBalance": btc_bal,
                "ethAddress": wallets['eth'], "ethBalance": eth_bal,
                "trxAddress": wallets['trx'], "trxBalance": trx_bal,
                "found": found
            }

            # SEND TO HTML via WEBSOCKET
            if user_session.socket:
                try:
                    await user_session.socket.send_json(data_packet)
                except:
                    pass # Socket might have closed

        except asyncio.CancelledError: break
        except Exception: await asyncio.sleep(0.1)

# --- WEBSOCKET ENDPOINT (The Missing Link) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Create a temporary session ID or handle auth if you send token
    # For now, we assume 1 connection = 1 anonymous fast session
    session_id = f"web_user_{int(time.time())}"
    
    session_obj = UserSession(session_id, speed=50) # Default speed 50
    session_obj.socket = websocket
    active_sessions[session_id] = session_obj

    mnemo = Mnemonic("english")
    async with aiohttp.ClientSession() as http_session:
        # Launch 5 workers for this connection
        workers = [asyncio.create_task(user_worker(session_obj, http_session, mnemo)) for _ in range(5)]
        session_obj.tasks = workers
        
        try:
            while True:
                # Keep connection alive, listen for stop commands
                data = await websocket.receive_text()
                if data == "stop":
                    session_obj.active = False
        except WebSocketDisconnect:
            session_obj.stop()
            del active_sessions[session_id]

# --- ADMIN ENDPOINTS (Unchanged) ---
@app.post("/admin/start/{user_id}")
async def start_user(user_id: str, speed: int = 50):
    if user_id not in active_sessions:
        active_sessions[user_id] = UserSession(user_id, speed)
        # Note: Admin started sessions usually run headless (no socket), 
        # unless we attach logic to save to file.
    return {"status": "started"}

@app.post("/admin/speed/{user_id}")
async def set_speed(user_id: str, speed: int):
    # Try to find loose match for web users
    target = None
    if user_id in active_sessions: target = active_sessions[user_id]
    else:
        # Allow partial match for "web_user" if admin types it
        for uid in active_sessions:
            if user_id in uid: target = active_sessions[uid]
    
    if target:
        target.limiter.set_rate(speed)
        return {"status": "updated", "speed": speed}
    return {"status": "not_found"}

@app.post("/admin/stop/{user_id}")
async def stop_user(user_id: str):
    # Kill logic similar to speed
    target_id = None
    if user_id in active_sessions: target_id = user_id
    else:
        for uid in active_sessions:
            if user_id in uid: target_id = uid
            
    if target_id:
        active_sessions[target_id].stop()
        del active_sessions[target_id]
        return {"status": "stopped"}
    return {"status": "not_found"}

@app.get("/admin/stats")
async def get_stats():
    data = []
    for uid, sess in active_sessions.items():
        elapsed = time.time() - sess.start_time
        real_speed = int(sess.attempts / elapsed) if elapsed > 0 else 0
        data.append({
            "user_id": uid, "target_speed": sess.limiter.rate_limit,
            "real_speed": real_speed, "total_attempts": sess.attempts, "found": sess.found
        })
    return data

@app.get("/panel", response_class=HTMLResponse)
async def admin_panel():
    # ... (Keep your Admin HTML string from previous turn here) ...
    # I am omitting it for brevity, but PASTE THE ADMIN HTML STRING HERE
    return """<!DOCTYPE html><html><body><h1>Use the Admin HTML from previous chat</h1></body></html>"""
