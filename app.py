from flask import Flask, request
import os
import ccxt
import json
from datetime import datetime
from threading import Thread
import requests
import time

app = Flask(__name__)

LOGS_DIRECTORY = "logs"
ACCOUNTS_FILE = "accounts.json"
CONFIG_FILE = "config.json"

if not os.path.exists(LOGS_DIRECTORY):
    os.makedirs(LOGS_DIRECTORY)

class account_c:
    def __init__(self, key, secret, password):
        self.exchange = ccxt.bitget({
            'apiKey': key,
            'secret': secret,
            'password': password,
            'enableRateLimit': True,
        })
        self.exchange.set_sandbox_mode(False)

    def parse(self, symbol):
        market = self.exchange.market(symbol)
        self.base = market['base']
        self.quote = market['quote']
        self.precision_amount = market['precision']['amount']
        self.precision_price = market['precision']['price']
        self.min_qty = market['limits']['amount']['min']
        self.symbol = symbol.replace("/", "")
        return market

    def format_amount(self, amount):
        return round(max(amount, self.min_qty), self.precision_amount)

    def position(self):
        positions = self.exchange.fetch_positions()
        return {p['symbol']: p for p in positions}

    def balance(self):
        return self.exchange.fetch_balance()

    def create_market_order(self, symbol, side, amount, params):
        return self.exchange.create_market_order(symbol, side, amount, params)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as file:
            return json.load(file)
    return {"active": True}

def save_config(config):
    with open(CONFIG_FILE, 'w') as file:
        json.dump(config, file, indent=4)

def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, 'r') as file:
            data = json.load(file)
            return {
                name: account_c(acc['key'], acc['secret'], acc['password'])
                for name, acc in data.items()
            }
    return {}

accounts = load_accounts()
config = load_config()

@app.route('/', methods=['GET'])
def root():
    return "Bot attivo"

@app.route('/config', methods=['POST'])
def update_config():
    new_config = request.get_json()
    if 'active' in new_config:
        config['active'] = new_config['active']
        save_config(config)
        return f"Stato bot aggiornato: {config['active']}"
    return "Chiave 'active' mancante nel JSON", 400

@app.route('/', methods=['POST'])
def webhook():
    if not config.get("active", True):
        return "Bot inattivo"

    data = request.get_json()
    message = data.get("message", "")
    if not message:
        return "Messaggio mancante", 400

    Thread(target=Alert, args=(message,)).start()
    return "Messaggio ricevuto", 200

def Alert(message):
    try:
        print(f"[{datetime.now()}] Messaggio ricevuto: {message}")

        parts = message.strip().split()
        if len(parts) < 3:
            print("Formato messaggio errato.")
            return

        name, cmd, symbol = parts[:3]
        amount = 0
        tf = '15m'
        leverage = 1

        for part in parts[3:]:
            if part.endswith('x'):
                leverage = int(part[:-1])
            elif part.endswith('m') or part.endswith('h'):
                tf = part
            elif '%' in part:
                amount = float(part.strip('%')) / 100
            elif part.startswith('$'):
                amount = float(part.strip('$'))
            elif part.replace('.', '', 1).isdigit():
                amount = float(part)

        if name not in accounts:
            print(f"Account {name} non trovato.")
            return

        acc = accounts[name]
        symbol = symbol.upper().replace('USDT', '/USDT')
        market = acc.parse(symbol)

        balance = acc.balance()
        usdt_balance = balance['total'].get('USDT', 0)

        qty = 0
        if amount > 1:
            qty = amount
        elif amount > 0:
            qty = (usdt_balance * amount) * leverage / market['info']['lastSz']
        else:
            qty = (usdt_balance * leverage) / market['info']['lastSz']

        qty = acc.format_amount(qty)

        if cmd.lower() == "close":
            positions = acc.position()
            pos = positions.get(symbol)
            if pos and float(pos['contracts']) > 0:
                side = 'sell' if pos['side'] == 'long' else 'buy'
                acc.create_market_order(symbol, side, float(pos['contracts']), {"reduceOnly": True})
                print(f"Posizione chiusa: {symbol}")
            else:
                print(f"Nessuna posizione attiva da chiudere per {symbol}")
        else:
            side = "buy" if cmd.lower() == "buy" else "sell"
            acc.create_market_order(symbol, side, qty, {"leverage": leverage})
            print(f"Ordine eseguito: {cmd.upper()} {symbol} Qty: {qty} Lev: {leverage}")

    except Exception as e:
        print(f"Errore: {e}")

# ✅ Keep-alive per evitare il deep sleep ogni 280 secondi
def keep_alive():
    while True:
        try:
            # ⚠️ Sostituisci con il tuo URL pubblico (es. su Replit, Fly.io, ecc.)
            requests.get("https://tuo-bot-url.replit.app/")
            print("[KEEP-ALIVE] Ping inviato con successo.")
        except Exception as e:
            print(f"[KEEP-ALIVE] Errore durante il ping: {e}")
        time.sleep(280)

if __name__ == '__main__':
    Thread(target=keep_alive, daemon=True).start()
    app.run(debug=False, host='0.0.0.0', port=8080)
