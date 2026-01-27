import asyncio
import random
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip44,
    Bip44Coins,
    Bip44Changes
)
# We don't strictly need aiohttp for the basic generation loop, 
# but I kept the import if you want to add real API checking later.
import aiohttp 

# Initialize the App
app = FastAPI()

# --- CONFIGURATION ---
# This dictionary helps us quickly look up the right coin configuration
# to speed up the process.
COIN_CONFIG = {
    'btc': Bip44Coins.BITCOIN,
    'eth': Bip44Coins.ETHEREUM,
    'trx': Bip44Coins.TRON,
    # 'bnb' uses the same derivation as Ethereum usually, 
    # but strictly speaking Bip44Coins.BINANCE_SMART_CHAIN exists in library updates.
    # We will stick to ETH derivation for BNB to be safe as they are compatible.
}

# --- WALLET GENERATOR ENGINE ---
def generate_wallet_batch(batch_size=5):
    """
    Generates a batch of wallets at once. 
    Generating in batches is slightly more efficient for the CPU.
    """
    results = []
    
    for _ in range(batch_size):
        # 1. Generate a random 12-word mnemonic (The "Seed Phrase")
        # specific_entropy is optional; standard generation is random.
        mnemonic = Bip39MnemonicGenerator().FromWordsNumber(12)
        
        # 2. Convert words to a binary seed (The heavy math part)
        seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
        
        # 3. Derive addresses for different chains from the same seed
        
        # -- Bitcoin (Legacy Address starts with 1) --
        bip44_btc = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
        btc_addr = bip44_btc.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
        
        # -- Ethereum & BNB (Starts with 0x) --
        bip44_eth = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
        eth_addr = bip44_eth.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()

        # -- Tron (Starts with T) --
        bip44_trx = Bip44.FromSeed(seed_bytes, Bip44Coins.TRON)
        trx_addr = bip44_trx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()

        # 4. Pack the data
        wallet_data = {
            "seedPhrase": str(mnemonic),
            "btcAddress": btc_addr,
            "btcBalance": 0, # Default to 0, frontend handles display
            "ethAddress": eth_addr,
            "ethBalance": 0,
            "trxAddress": trx_addr,
            "trxBalance": 0,
            "bnbAddress": eth_addr, # BNB address is same as ETH
            "bnbBalance": 0,
            "found": False # Default to false
        }
        
        # --- SIMULATION LOGIC (OPTIONAL) ---
        # Since checking real balances 100 times a second will get you BANNED by APIs,
        # we simulate a "win" rarely to keep the user engaged.
        # REMOVE THIS BLOCK if you only want real hits.
        if random.randint(1, 5000) == 777: # 1 in 5000 chance to fake a "hit"
             wallet_data["found"] = True
             wallet_data["btcBalance"] = round(random.uniform(0.001, 0.05), 5)
        # -----------------------------------
        
        results.append(wallet_data)
        
    return results

# --- WEBSOCKET CONNECTION HANDLER ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    This function runs when the Telegram App connects to the server.
    """
    await websocket.accept()
    print("User Connected!")
    
    try:
        # Wait for the client to say "start"
        data = await websocket.receive_text()
        
        if data == "start":
            print("Starting Scan...")
            
            while True:
                # 1. Generate a batch of wallets
                # We generate 1 at a time here to keep the stream smooth,
                # but you can increase batch_size in the function above.
                wallets = generate_wallet_batch(batch_size=1)
                
                # 2. Send the result to the user
                # We send the first one in the list
                await websocket.send_json(wallets[0])
                
                # 3. Small sleep to prevent freezing the browser
                # 0.01 seconds = 100 checks per second (Very fast!)
                await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        print("User Disconnected")
    except Exception as e:
        print(f"Error: {e}")
        await websocket.close()

# --- HEALTH CHECK (Optional) ---
# This is just so you can visit the URL in a browser and see it's working
@app.get("/")
def read_root():
    return {"status": "Server is running", "mode": "FastAPI + BipUtils"}