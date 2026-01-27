import asyncio
import aiohttp
import os
from fastapi import FastAPI
from mnemonic import Mnemonic
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from colorama import Fore, init
import time

# --- INIT ---
app = FastAPI()
init(autoreset=True)

# --- CONFIG ---
CONCURRENT_TASKS = 50  # Number of simultaneous checks (Async makes this huge!)
ENABLE_BTC = True
ENABLE_ETH = True  # Also covers BSC
ENABLE_TRX = True

# --- API ENDPOINTS ---
# We use rotating endpoints or high-capacity public RPCs
ETH_RPC = "https://eth.llamarpc.com"
BSC_RPC = "https://bsc-dataseed.binance.org/"
TRX_API = "https://api.trongrid.io/v1/accounts/{}"
BTC_API = "https://blockstream.info/api/address/{}"

# --- STATE ---
stats = {
    "attempts": 0,
    "start_time": time.time(),
    "found_count": 0
}

# --- DERIVATION (CPU BOUND) ---
def derive_wallets(phrase):
    try:
        seed_bytes = Bip39SeedGenerator(phrase).Generate()
        wallets = {}
        
        # CPU heavy tasks - simplistic derivation for speed
        if ENABLE_BTC:
            bip_btc = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
            wallets['btc'] = bip_btc.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()

        if ENABLE_ETH:
            bip_eth = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
            wallets['eth'] = bip_eth.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()

        if ENABLE_TRX:
            bip_trx = Bip44.FromSeed(seed_bytes, Bip44Coins.TRON)
            wallets['trx'] = bip_trx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
            
        return wallets
    except Exception:
        return None

# --- ASYNC CHECKERS (IO BOUND) ---
async def check_balance(session, url, method="GET", json_data=None):
    try:
        if method == "POST":
            async with session.post(url, json=json_data, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
        else:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
    except:
        pass
    return None

async def worker(mnemo, session):
    """Single scan cycle"""
    while True:
        try:
            # 1. Generate & Derive (Sync operation)
            phrase = mnemo.generate(strength=128)
            wallets = derive_wallets(phrase)
            if not wallets: continue

            # 2. Prepare Async Checks
            tasks = []
            
            # BTC
            if ENABLE_BTC:
                tasks.append(check_balance(session, BTC_API.format(wallets['btc'])))
            else:
                tasks.append(asyncio.sleep(0)) # Placeholder

            # ETH
            if ENABLE_ETH:
                payload = {"jsonrpc":"2.0","method":"eth_getBalance","params":[wallets['eth'], "latest"],"id":1}
                tasks.append(check_balance(session, ETH_RPC, "POST", payload))
            else:
                tasks.append(asyncio.sleep(0))
            
            # TRX
            if ENABLE_TRX:
                tasks.append(check_balance(session, TRX_API.format(wallets['trx'])))
            else:
                tasks.append(asyncio.sleep(0))

            # 3. Fire all requests at once
            results = await asyncio.gather(*tasks)
            
            # 4. Analyze Results
            found_msg = ""
            is_hit = False
            
            # BTC Result
            btc_res = results[0]
            if btc_res:
                bal = (btc_res.get('chain_stats', {}).get('funded_txo_sum', 0) - btc_res.get('chain_stats', {}).get('spent_txo_sum', 0)) / 100000000
                if bal > 0:
                    found_msg += f"BTC: {bal} | "
                    is_hit = True

            # ETH Result
            eth_res = results[1]
            if eth_res and "result" in eth_res:
                bal = int(eth_res["result"], 16) / 10**18
                if bal > 0:
                    found_msg += f"ETH: {bal} | "
                    is_hit = True

            # TRX Result
            trx_res = results[2]
            if trx_res and trx_res.get('data'):
                bal = trx_res['data'][0]['balance'] / 1000000
                if bal > 0:
                    found_msg += f"TRX: {bal} | "
                    is_hit = True

            # 5. Reporting
            stats['attempts'] += 1
            if stats['attempts'] % 100 == 0:
                elapsed = time.time() - stats['start_time']
                print(f"{Fore.CYAN}[RUNNING] {stats['attempts']} checked ({int(stats['attempts']/elapsed)}/sec) | Last: {phrase.split()[0]}...")

            if is_hit:
                print(f"\n{Fore.GREEN}!!! FOUND !!!\nSeed: {phrase}\n{found_msg}\n")
                with open("hits.txt", "a") as f:
                    f.write(f"{phrase} | {found_msg}\n")
        
        except Exception as e:
            # print(e) # Debug only
            await asyncio.sleep(0.1)

async def scanner_loop():
    """Background task manager"""
    print(Fore.YELLOW + "Starting Async Scanner...")
    mnemo = Mnemonic("english")
    
    # Create a persistent session for speed
    async with aiohttp.ClientSession() as session:
        # Launch workers
        tasks = [worker(mnemo, session) for _ in range(CONCURRENT_TASKS)]
        await asyncio.gather(*tasks)

# --- FASTAPI SERVER (Keep-Alive for Render) ---
@app.on_event("startup")
async def startup_event():
    # Run scanner in background without blocking server
    asyncio.create_task(scanner_loop())

@app.get("/")
def read_root():
    # Health check endpoint for Render
    elapsed = time.time() - stats['start_time']
    rate = int(stats['attempts'] / elapsed) if elapsed > 0 else 0
    return {
        "status": "active", 
        "scanned": stats['attempts'], 
        "rate_per_sec": rate,
        "hits": stats['found_count']
    }
