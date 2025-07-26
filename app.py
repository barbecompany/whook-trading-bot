import ccxt
from flask import Flask, request, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from threading import Timer
import os
import time
import json
import copy
import logging
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from pprint import pprint


def fixVersionFormat(version)->str:
    vl = version.split(".")
    return f'{vl[0]}.{vl[1]}.{vl[2].zfill(3)}'

minCCXTversion = '4.2.82'
CCXTversion = fixVersionFormat(ccxt.__version__)
print('CCXT Version:', ccxt.__version__)
if(CCXTversion < fixVersionFormat(minCCXTversion)):
    print('\n============== * WARNING * ==============')
    print('WHOOK requires CCXT version', minCCXTversion,' or higher.')
    print('While it may run with earlier versions wrong behaviors are expected to happen.')
    print('Please update CCXT.')
    print('============== * WARNING * ==============\n')
    

###################
##### Globals #####
###################

verbose = False
debug_order = False
SHOW_BALANCE = False
SHOW_LIQUIDATION = False
SHOW_BREAKEVEN = True
SHOW_REALIZEDPNL = False
SHOW_ENTRYPRICE = False
USE_PROXY = False
PORT = int(os.environ.get('PORT', 8080))  # Koyeb compatibility
PROXY_PORT = 50000
ALERT_TIMEOUT = 60 * 3
ORDER_TIMEOUT = 40
REFRESH_POSITIONS_FREQUENCY = 5 * 60
UPDATE_ORDERS_FREQUENCY = 0.25
LOGS_DIRECTORY = 'logs'
MARGIN_MODE_NONE = '------'

#### Open config file #####

def writeConfig():
    config_data = {
        "ALERT_TIMEOUT": ALERT_TIMEOUT,
        "ORDER_TIMEOUT": ORDER_TIMEOUT,
        "REFRESH_POSITIONS_FREQUENCY": REFRESH_POSITIONS_FREQUENCY,
        "UPDATE_ORDERS_FREQUENCY": UPDATE_ORDERS_FREQUENCY,
        "VERBOSE": verbose,
        "SHOW_BALANCE": SHOW_BALANCE,
        "SHOW_REALIZEDPNL": SHOW_REALIZEDPNL,
        "SHOW_ENTRYPRICE": SHOW_ENTRYPRICE,
        "SHOW_LIQUIDATION": SHOW_LIQUIDATION,
        "SHOW_BREAKEVEN": SHOW_BREAKEVEN,
        "LOGS_DIRECTORY": LOGS_DIRECTORY,
        "USE_PROXY": USE_PROXY,
        "PROXY_PORT": PROXY_PORT
    }
    
    with open('config.json', 'w') as f:
        json.dump([config_data], f, indent=2)

try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)[0]
except FileNotFoundError:
    writeConfig()
    print("Config file created.\n----------------------------")
else:
    # Parse config
    globals().update({k: v for k, v in config.items() if k in globals()})
    writeConfig()


##### Utils #####

def dateString():
    return datetime.today().strftime("%Y/%m/%d")

def timeNow():
    return time.strftime("%H:%M:%S")

def roundUpTick(value: float, tick: str):
    if type(tick) is not str: tick = str(tick)
    if type(value) is not Decimal: value = Decimal(value)
    return float(value.quantize(Decimal(tick), ROUND_CEILING))

def roundDownTick(value: float, tick: str):
    if type(tick) is not str: tick = str(tick)
    if type(value) is not Decimal: value = Decimal(value)
    return float(value.quantize(Decimal(tick), ROUND_FLOOR))

def roundToTick(value: float, tick: float):
    if type(tick) is not str: tick = str(tick)
    if type(value) is not Decimal: value = Decimal(value)
    return float(value.quantize(Decimal(tick), ROUND_HALF_EVEN))

class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)

# Simplified account class for Koyeb
class account_c:
    def __init__(self, exchange=None, name='default', apiKey=None, secret=None, password=None, marginMode=None, settleCoin=None):
        self.accountName = name
        self.positionslist = []
        self.MARGIN_MODE = 'cross' if (marginMode != None and marginMode.lower() == 'cross') else 'isolated'
        self.SETTLE_COIN = 'USDT' if(settleCoin == None) else settleCoin

        if(exchange == None):
            raise ValueError('Exchange not defined')
        
        if(exchange.lower() == 'bitget'):
            self.exchange = ccxt.bitget({
                "apiKey": apiKey,
                "secret": secret,
                'password': password,
                "options": {'defaultType': 'swap', 'defaultMarginMode': self.MARGIN_MODE, 'adjustForTimeDifference': True},
                "enableRateLimit": False
            })
        else:
            raise ValueError('Unsupported exchange')

        if(self.exchange == None):
            raise ValueError('Exchange creation failed')
        
        # Load basic markets info
        self.markets = {}
        try:
            markets = self.exchange.load_markets()
            for key, market in markets.items():
                if market.get('settle') == self.SETTLE_COIN:
                    self.markets[key] = market
        except Exception as e:
            print(f"Error loading markets: {e}")
            
        self.refreshPositions(True)

    def print(self, *args, sep=" ", **kwargs):
        print(timeNow(), '['+ self.accountName +'/'+ self.exchange.id +']', *args, sep=sep, **kwargs)

    def fetchBalance(self):
        try:
            params = {"settle": self.SETTLE_COIN}
            response = self.exchange.fetch_balance(params)
            if(response.get(self.SETTLE_COIN) == None):
                return {'free': 0.0, 'used': 0.0, 'total': 0.0}
            return response.get(self.SETTLE_COIN)
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return {'free': 0.0, 'used': 0.0, 'total': 0.0}

    def fetchAvailableBalance(self)->float:
        return float(self.fetchBalance().get('free'))

    def fetchAveragePrice(self, symbol)->float:
        try:
            orderbook = self.exchange.fetch_order_book(symbol)
            bid = orderbook['bids'][0][0] if len(orderbook['bids']) > 0 else None
            ask = orderbook['asks'][0][0] if len(orderbook['asks']) > 0 else None
            if(bid == None and ask == None):
                raise ValueError("Couldn't fetch orderbook")
            if(bid == None): bid = ask
            if(ask == None): ask = bid
            return (bid + ask) * 0.5
        except Exception as e:
            print(f"Error fetching price: {e}")
            return 0.0

    def findSymbolFromPairName(self, pairString):
        paircmd = pairString.upper()
        if(paircmd.endswith('.P')):
            paircmd = paircmd[:-2]

        if '/' not in paircmd and paircmd.endswith(self.SETTLE_COIN):
            paircmd = paircmd[:-len(self.SETTLE_COIN)]
            paircmd += '/' + self.SETTLE_COIN + ':' + self.SETTLE_COIN

        if '/' in paircmd and not paircmd.endswith(':' + self.SETTLE_COIN):
            paircmd += ':' + self.SETTLE_COIN

        if paircmd in self.markets:
            return paircmd

        for symbol in self.markets:
            if symbol == paircmd:
                return symbol
        return None

    def contractsFromUSDT(self, symbol, amount, price, leverage=1.0)->float:
        try:
            if symbol not in self.markets:
                return 0.0
            contractSize = self.markets[symbol].get('contractSize', 1.0)
            if contractSize is None:
                contractSize = 1.0
            return (amount * leverage) / (contractSize * price)
        except Exception as e:
            print(f"Error calculating contracts: {e}")
            return 0.0

    def refreshPositions(self, v=verbose):
        try:
            symbols = list(self.markets.keys()) if self.markets else None
            positions = self.exchange.fetch_positions(symbols, params={'settle': self.SETTLE_COIN})
            
            # Filter active positions
            activePositions = []
            for position in positions:
                if position.get('contracts', 0.0) != 0.0:
                    activePositions.append(position)
            
            self.positionslist = activePositions
            
            if v:
                numPositions = len(activePositions)
                print('------------------------------')
                if SHOW_BALANCE:
                    balance = self.fetchBalance()
                    print(f"  {numPositions} positions found. Balance: {balance['total']:.2f}$ - Available {balance['free']:.2f}$")
                else:
                    print(f"  {numPositions} positions found.")
                
                for position in activePositions:
                    symbol = position.get('symbol', 'Unknown')
                    side = position.get('side', 'Unknown')
                    contracts = position.get('contracts', 0.0)
                    unrealizedPnl = position.get('unrealizedPnl', 0.0)
                    print(f"  {symbol} * {side} * {contracts} * {unrealizedPnl:.2f}$")
                print('------------------------------')
                
        except Exception as e:
            print(f"Error refreshing positions: {e}")
            self.positionslist = []

    def proccessAlert(self, alert: dict):
        self.print(' ')
        self.print(" ALERT:", alert['alert'])
        self.print('----------------------------')

        try:
            available = self.fetchAvailableBalance() * 0.985
        except Exception as e:
            self.print(" * E: Couldn't fetch balance: Cancelling", e)
            return

        # Parse alert message
        tokens = alert['alert'].split()
        symbol = None
        command = None
        amount = None

        for token in tokens:
            if(self.findSymbolFromPairName(token) != None):
                symbol = self.findSymbolFromPairName(token)
            elif token.lower() == "buy":
                command = 'buy'
            elif token.lower() == "sell":
                command = 'sell'
            elif token.lower() == "close":
                command = 'close'
            elif token.startswith('$'):
                try:
                    amount = float(token[1:])
                except:
                    amount = None

        if not symbol:
            self.print(" * E: Symbol not found in alert")
            return
            
        if not command:
            self.print(" * E: Command not found in alert")
            return

        # Handle close command
        if command == 'close':
            try:
                self.refreshPositions(False)
                for position in self.positionslist:
                    if position.get('symbol') == symbol:
                        contracts = position.get('contracts', 0.0)
                        side = position.get('side', '')
                        if side == 'long':
                            result = self.exchange.create_order(symbol, 'market', 'sell', contracts)
                            self.print(f" * Close order successful: {symbol} sell {contracts}")
                        elif side == 'short':
                            result = self.exchange.create_order(symbol, 'market', 'buy', contracts)
                            self.print(f" * Close order successful: {symbol} buy {contracts}")
                        return
                self.print(f" * No position found to close for {symbol}")
                return
            except Exception as e:
                self.print(f" * E: Close order failed: {e}")
                return

        if not amount:
            self.print(" * E: Amount not found in alert")
            return

        # Execute buy/sell order
        try:
            price = self.fetchAveragePrice(symbol)
            if price <= 0:
                self.print(" * E: Invalid price")
                return
                
            quantity = self.contractsFromUSDT(symbol, amount, price, 1)
            
            if quantity <= 0:
                self.print(" * E: Invalid quantity calculated")
                return
            
            # Check minimum order size
            minQty = 0.0001  # Basic minimum for most pairs
            if symbol in self.markets:
                minAmount = self.markets[symbol].get('limits', {}).get('amount', {}).get('min', minQty)
                if minAmount:
                    minQty = minAmount
            
            if quantity < minQty:
                self.print(f" * E: Order too small: {quantity} < {minQty}")
                return
            
            if command == 'buy':
                result = self.exchange.create_order(symbol, 'market', 'buy', quantity)
                self.print(f" * Buy order successful: {symbol} buy {quantity} at price {price}")
            elif command == 'sell':
                result = self.exchange.create_order(symbol, 'market', 'sell', quantity)
                self.print(f" * Sell order successful: {symbol} sell {quantity} at price {price}")
                
        except Exception as e:
            self.print(f" * E: Order failed: {e}")

accounts = []

def Alert(data):
    account = None
    lines = data.split("\n")
    for line in lines:
        line = line.rstrip('\n')
        if(len(line) == 0):
            continue
        if(line[:2] == '//'):
            continue
        
        tokens = line.split()
        for token in tokens:
            for a in accounts:
                if(token.lower() == a.accountName.lower()):
                    account = a
                    break
        
        if(account == None): 
            print(timeNow(), ' * E: Account ID not found. ALERT:', line)
            continue
        
        alert = {
            'alert': line.replace('\n', ''),
            'timestamp': time.monotonic()
        }
        
        account.proccessAlert(alert)

def refreshPositions():
    for account in accounts:
        account.refreshPositions()

def generatePositionsString()->str:
    msg = ''
    for account in accounts:
        account.refreshPositions()
        numPositions = len(account.positionslist)
        balanceString = ''
        if SHOW_BALANCE:
            try:
                balance = account.fetchBalance()
                balanceString = f" * Balance: {balance['total']:.2f}$ - Available {balance['free']:.2f}$"
            except:
                balanceString = ''

        msg += '---------------------\n'
        msg += f'Refreshing positions {account.accountName}: {numPositions} positions found{balanceString}\n'
        
        if numPositions > 0:
            for position in account.positionslist:
                symbol = position.get('symbol', 'Unknown')
                side = position.get('side', 'Unknown')
                contracts = position.get('contracts', 0.0)
                unrealizedPnl = position.get('unrealizedPnl', 0.0)
                msg += f"{symbol} * {side} * {contracts} * {unrealizedPnl:.2f}$\n"

    return msg

###################
#### Initialize ###
###################

print('----------------------------')

#### Open accounts file ###

try:
    with open('accounts.json', 'r') as accounts_file:
        accounts_data = json.load(accounts_file)
except FileNotFoundError:
    accounts_template = [
        {
            "ACCOUNT_ID": "your_account_name",
            "EXCHANGE": "bitget",
            "API_KEY": "your_api_key",
            "SECRET_KEY": "your_secret_key",
            "PASSWORD": "your_API_password",
            "MARGIN_MODE": "isolated"
        }
    ]
    with open('accounts.json', 'w') as f:
        json.dump(accounts_template, f, indent=2)
    print("File 'accounts.json' not found. Template created. Please fill your API Keys into the file and try again")
    print("Exiting.")
    raise SystemExit()

for ac in accounts_data:
    exchange = ac.get('EXCHANGE')
    if(exchange == None):
        print(" * ERROR PARSING ACCOUNT INFORMATION: EXCHANGE")
        continue

    account_id = ac.get('ACCOUNT_ID')
    if(account_id == None):
        print(" * ERROR PARSING ACCOUNT INFORMATION: ACCOUNT_ID")
        continue

    api_key = ac.get('API_KEY')
    if(api_key == None):
        print(" * ERROR PARSING ACCOUNT INFORMATION: API_KEY")
        continue

    secret_key = ac.get('SECRET_KEY')
    if(secret_key == None):
        print(" * ERROR PARSING ACCOUNT INFORMATION: SECRET_KEY")
        continue

    password = ac.get('PASSWORD')
    if(password == None):
        password = ""

    marginMode = ac.get('MARGIN_MODE')
    settleCoin = ac.get('SETTLE_COIN')

    print(timeNow(), " Initializing account: [", account_id, "] in [", exchange, ']')
    try:
        account = account_c(exchange, account_id, api_key, secret_key, password, marginMode, settleCoin)
    except Exception as e:
        print('Account creation failed:', e)
        print('------------------------------')
    else:
        accounts.append(account)

if(len(accounts) == 0):
    print(" * FATAL ERROR: No valid accounts found. Please edit 'accounts.json' and introduce your API keys")
    raise SystemExit()

############################################

# define the webhook server
app = Flask(__name__)
# silencing flask useless spam
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
log.disabled = True

@app.route('/whook', methods=['GET','POST'])
def webhook():
    if request.method == 'POST':
        content_type = request.headers.get('Content-Type')
        if content_type == 'application/json':
            data = request.get_json()
            if data and 'update_id' in data:
                if 'message' in data:
                    chat_id = data['message']['chat']['id']
                    message = data['message']['text']
                    print("Received message from chat_id", chat_id, ':', message)
                return 'Telegram message processed', 200
            return 'success', 200
        
        # Standard alert
        data = request.get_data(as_text=True)
        Alert(data)
        return 'success', 200
    
    if request.method == 'GET':
        response = request.args.get('response')
        if(response == None):
            msg = generatePositionsString()
            return app.response_class(f"<pre>{msg}</pre>", mimetype='text/html; charset=utf-8')
        
        if response == 'whook':
            return 'WHOOKITYWOOK'
        
        return 'Not found'
        
    else:
        abort(400)

# start the webhook server
if __name__ == '__main__':
    print(" * Listening on port", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)