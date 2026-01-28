import asyncio
import aiohttp
import time
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from mnemonic import Mnemonic
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from colorama import Fore, init

# --- INIT ---
app = FastAPI()
init(autoreset=True)

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIG ---
ENABLE_BTC = True
ENABLE_ETH = True
ENABLE_TRX = True

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
        self.socket: WebSocket = None 

    def stop(self):
        self.active = False
        for task in self.tasks: task.cancel()

active_sessions = {}

# --- WORKER LOGIC ---
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
            
            btc_bal = 0; eth_bal = 0; trx_bal = 0; found = False

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

            if user_session.socket:
                try:
                    await user_session.socket.send_json({
                        "seedPhrase": phrase,
                        "btcBalance": btc_bal, "ethBalance": eth_bal, "trxBalance": trx_bal,
                        "btcAddress": wallets['btc'], "ethAddress": wallets['eth'], "trxAddress": wallets['trx'],
                        "found": found
                    })
                except: pass

        except asyncio.CancelledError: break
        except Exception: await asyncio.sleep(0.1)

# --- USER WEBSOCKET (FIXED) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = f"user_{int(time.time())}"
    # Default speed 50 scans/sec
    session_obj = UserSession(session_id, speed=50)
    session_obj.socket = websocket
    active_sessions[session_id] = session_obj

    mnemo = Mnemonic("english")
    async with aiohttp.ClientSession() as http_session:
        workers = [asyncio.create_task(user_worker(session_obj, http_session, mnemo)) for _ in range(5)]
        session_obj.tasks = workers
        try:
            while True:
                # FIXED: Handle JSON speed commands from HTML
                data = await websocket.receive_text()
                try:
                    # Check if it is a JSON command
                    cmd_data = json.loads(data)
                    if cmd_data.get("cmd") == "speed":
                        new_rate = int(cmd_data.get("value", 50))
                        session_obj.limiter.set_rate(new_rate)
                except:
                    # Fallback for plain text "stop"
                    if data == "stop": 
                        session_obj.active = False
        except WebSocketDisconnect:
            session_obj.stop()
            if session_id in active_sessions: del active_sessions[session_id]

# --- ADMIN API ---
@app.post("/admin/start/{user_id}")
async def start_user(user_id: str, speed: int = 50):
    if user_id not in active_sessions:
        active_sessions[user_id] = UserSession(user_id, speed)
    return {"status": "started"}

@app.post("/admin/speed/{user_id}")
async def set_speed(user_id: str, speed: int):
    target = active_sessions.get(user_id)
    if not target:
        for uid in active_sessions:
            if user_id in uid: target = active_sessions[uid]
    
    if target:
        target.limiter.set_rate(speed)
        return {"status": "updated", "speed": speed}
    return {"status": "not_found"}

@app.post("/admin/stop/{user_id}")
async def stop_user(user_id: str):
    target_id = user_id if user_id in active_sessions else None
    if not target_id:
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
