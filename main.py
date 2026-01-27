import asyncio
import aiohttp
import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from mnemonic import Mnemonic
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from colorama import Fore, init

# --- INIT ---
app = FastAPI()
init(autoreset=True)
templates = Jinja2Templates(directory="templates")

# --- CONFIG ---
ENABLE_BTC = True
ENABLE_ETH = True
ENABLE_TRX = True

# --- API ENDPOINTS ---
ETH_RPC = "https://eth.llamarpc.com"
TRX_API = "https://api.trongrid.io/v1/accounts/{}"
BTC_API = "https://blockstream.info/api/address/{}"

# --- RATE LIMITER CLASS ---
class RateLimiter:
    """Controls the speed (actions per second)"""
    def __init__(self, rate_limit: int):
        self.rate_limit = rate_limit  # Max calls per second
        self.tokens = rate_limit
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def wait_for_token(self):
        if self.rate_limit <= 0: # 0 means pause
            while self.rate_limit <= 0:
                await asyncio.sleep(1)
            return

        if self.rate_limit >= 1000: # High speed, practically unlimited
            return

        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            
            # Refill tokens based on time elapsed
            new_tokens = elapsed * self.rate_limit
            if new_tokens > 0:
                self.tokens = min(self.rate_limit, self.tokens + new_tokens)
                self.last_refill = now
            
            # Consume token
            if self.tokens >= 1:
                self.tokens -= 1
            else:
                # Calculate wait time needed for 1 token
                wait_time = (1 - self.tokens) / self.rate_limit
                await asyncio.sleep(wait_time)
                self.tokens = 0
                self.last_refill = time.monotonic()

    def set_rate(self, new_rate: int):
        self.rate_limit = new_rate

# --- USER SESSION CLASS ---
class UserSession:
    def __init__(self, user_id: str, speed: int = 50):
        self.user_id = user_id
        self.limiter = RateLimiter(speed)
        self.active = True
        self.attempts = 0
        self.found = 0
        self.start_time = time.time()
        self.tasks = []

    def stop(self):
        self.active = False
        for task in self.tasks:
            task.cancel()

# --- GLOBAL STORE ---
active_sessions = {}  # { "user_x": UserSessionObj }
session_lock = asyncio.Lock()

# --- DERIVATION LOGIC ---
def derive_wallets(phrase):
    try:
        seed_bytes = Bip39SeedGenerator(phrase).Generate()
        wallets = {}
        if ENABLE_BTC:
            wallets['btc'] = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        if ENABLE_ETH:
            wallets['eth'] = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        if ENABLE_TRX:
            wallets['trx'] = Bip44.FromSeed(seed_bytes, Bip44Coins.TRON).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        return wallets
    except:
        return None

# --- WORKER LOGIC ---
async def check_balance(session, url, method="GET", json_data=None):
    try:
        if method == "POST":
            async with session.post(url, json=json_data, timeout=5) as response:
                if response.status == 200: return await response.json()
        else:
            async with session.get(url, timeout=5) as response:
                if response.status == 200: return await response.json()
    except:
        pass
    return None

async def user_worker(user_session: UserSession, session: aiohttp.ClientSession, mnemo: Mnemonic):
    while user_session.active:
        try:
            # 1. Rate Limit Check
            await user_session.limiter.wait_for_token()

            # 2. Logic
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

            # Check logic (simplified for brevity)
            is_hit = False
            found_msg = ""
            
            # BTC check
            if results[0]:
                 bal = (results[0].get('chain_stats', {}).get('funded_txo_sum', 0) - results[0].get('chain_stats', {}).get('spent_txo_sum', 0)) / 100000000
                 if bal > 0: is_hit = True; found_msg += f"BTC: {bal} "

            # ETH check
            if results[1] and "result" in results[1]:
                bal = int(results[1]["result"], 16) / 10**18
                if bal > 0: is_hit = True; found_msg += f"ETH: {bal} "

            # TRX check
            if results[2] and results[2].get('data'):
                bal = results[2]['data'][0]['balance'] / 1000000
                if bal > 0: is_hit = True; found_msg += f"TRX: {bal} "

            user_session.attempts += 1
            
            if is_hit:
                user_session.found += 1
                print(f"{Fore.GREEN}[USER: {user_session.user_id}] FOUND! {phrase} | {found_msg}")
                with open("hits.txt", "a") as f:
                    f.write(f"{user_session.user_id} | {phrase} | {found_msg}\n")

        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.1)

# --- SESSION CONTROLLER ---
async def start_user_session(user_id: str, speed: int):
    async with session_lock:
        if user_id in active_sessions:
            return # Already exists
        
        session_obj = UserSession(user_id, speed)
        active_sessions[user_id] = session_obj

    # Start workers in background
    mnemo = Mnemonic("english")
    async with aiohttp.ClientSession() as http_session:
        # Launch 10 concurrent threads per user, but RateLimiter will control the actual speed
        workers = [asyncio.create_task(user_worker(session_obj, http_session, mnemo)) for _ in range(10)]
        session_obj.tasks = workers
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            pass

# --- ADMIN API ENDPOINTS ---

@app.post("/admin/start/{user_id}")
async def start_user(user_id: str, speed: int = 50):
    """Start a new scanning session for a user"""
    asyncio.create_task(start_user_session(user_id, speed))
    return {"status": "started", "user": user_id, "speed": speed}

@app.post("/admin/speed/{user_id}")
async def set_speed(user_id: str, speed: int):
    """Update speed for an existing user immediately"""
    if user_id not in active_sessions:
        raise HTTPException(status_code=404, detail="User not active")
    
    active_sessions[user_id].limiter.set_rate(speed)
    return {"status": "updated", "user": user_id, "new_speed": speed}

@app.post("/admin/stop/{user_id}")
async def stop_user(user_id: str):
    """Kill a user session"""
    if user_id in active_sessions:
        active_sessions[user_id].stop()
        del active_sessions[user_id]
        return {"status": "stopped", "user": user_id}
    return {"status": "not_found"}

@app.get("/admin/stats")
async def get_stats():
    """Get live stats for the admin panel"""
    data = []
    for uid, sess in active_sessions.items():
        elapsed = time.time() - sess.start_time
        real_speed = int(sess.attempts / elapsed) if elapsed > 0 else 0
        data.append({
            "user_id": uid,
            "target_speed": sess.limiter.rate_limit,
            "real_speed": real_speed,
            "total_attempts": sess.attempts,
            "found": sess.found
        })
    return data

# --- ADMIN PANEL HTML ---
@app.get("/panel", response_class=HTMLResponse)
async def admin_panel(request: Request):
    # Simple HTML Dashboard
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Control | BHS</title>
        <style>
            body { background: #111; color: #0f0; font-family: monospace; padding: 20px; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th, td { border: 1px solid #333; padding: 10px; text-align: left; }
            th { color: #fff; }
            .btn { background: #333; color: white; border: none; padding: 5px 10px; cursor: pointer; }
            .btn:hover { background: #555; }
            input { background: #222; border: 1px solid #444; color: white; padding: 5px; }
        </style>
    </head>
    <body>
        <h1>/// ADMIN COMMAND NODE ///</h1>
        
        <div>
            <h3>Start New Session</h3>
            <input type="text" id="newUser" placeholder="User ID (e.g. user_x)">
            <input type="number" id="newSpeed" placeholder="Speed (scans/s)" value="50">
            <button class="btn" onclick="startSession()">BOOT NODE</button>
        </div>

        <h3>Active Nodes</h3>
        <table id="statsTable">
            <thead>
                <tr>
                    <th>User ID</th>
                    <th>Target Speed</th>
                    <th>Real Speed</th>
                    <th>Total Scans</th>
                    <th>Hits</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody id="tableBody"></tbody>
        </table>

        <script>
            async function refresh() {
                let res = await fetch('/admin/stats');
                let data = await res.json();
                let html = '';
                data.forEach(u => {
                    html += `<tr>
                        <td>${u.user_id}</td>
                        <td>
                            <input type="number" value="${u.target_speed}" 
                                onchange="updateSpeed('${u.user_id}', this.value)" style="width:60px">
                        </td>
                        <td>${u.real_speed}/s</td>
                        <td>${u.total_attempts}</td>
                        <td style="color:${u.found > 0 ? 'red' : '#0f0'}">${u.found}</td>
                        <td><button class="btn" style="color:red" onclick="stopUser('${u.user_id}')">KILL</button></td>
                    </tr>`;
                });
                document.getElementById('tableBody').innerHTML = html;
            }

            async function startSession() {
                let uid = document.getElementById('newUser').value;
                let spd = document.getElementById('newSpeed').value;
                if(!uid) return alert("Enter User ID");
                await fetch(`/admin/start/${uid}?speed=${spd}`, {method:'POST'});
                refresh();
            }

            async function updateSpeed(uid, newSpeed) {
                await fetch(`/admin/speed/${uid}?speed=${newSpeed}`, {method:'POST'});
            }

            async function stopUser(uid) {
                if(confirm('Terminate session?')) {
                    await fetch(`/admin/stop/${uid}`, {method:'POST'});
                    refresh();
                }
            }

            setInterval(refresh, 2000); // Auto refresh every 2s
            refresh();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# Start default user on boot
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(start_user_session("default_admin", 100))
