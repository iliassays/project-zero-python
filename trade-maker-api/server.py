import asyncio
import datetime
from http.client import HTTPException
import math
import uvicorn
import pymongo
from typing import List
from bson.objectid import ObjectId
from contextlib import asynccontextmanager
from pydantic_settings import BaseSettings
from fastapi import FastAPI, Request
from pydantic import BaseModel
from ib_api import REST
import logging
import pytz

class AlertItem(BaseModel):
    algo: str
    symbol: str
    alert_type: str
    signal: str
    interval: str
    exchange: str
    source: str
    close: str

    
class ScaleAlertItem(AlertItem):
    scale_amount: float

class SubscribedStock(BaseModel):
    symbol: str
    type: str
    interval: str
    algo: str
    type: str
    investment_long: float
    investment_short: float
    is_active: bool
    
class UserConfig(BaseModel):
    username: str
    trading_account: str
    host: str = ""
    port: int
    subscribed_stocks: List[SubscribedStock]
    is_active: bool
    is_account_shortable: bool
    is_scaling_enabled: bool
    is_tracking_only: bool
    is_outsideRTH_enabled: bool

class Event(BaseModel):
    signal: str
    status: str
    side: str
    contracts: int
    price: float

class Trade(BaseModel):
    symbol: str
    alert_type: str
    events: List[Event]
    profitOrLoss: float = 0
    profitOrLossPercentage: float = 0
    contracts: int = 0
    open_positions: int = 0
    order_status: str

class TradeFilter(BaseModel):
    ticker: str
    algo: str

class Settings(BaseSettings):
    ib_gateway_host: str = "127.0.0.1"
    ib_gateway_port: str = "7497"
    timezone: str = "US/Eastern"
    timeformat: str = "client-2"

# Initialize logging
logging.basicConfig(level=logging.INFO)

settings = Settings()
client = pymongo.MongoClient("mongodb://mongo:27017")
db = client['project_zero']
running_trade_collection = db['running_trades']
trade_history_collection = db['trade_history']
user_configs_collection = db['user_configs']
trade_failures_collection = db['trade_failures']

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
def read_root():
    return {"Hello": "YES"}

@app.get("/trades/")
async def trades(trade_filter: TradeFilter):
    ticker = trade_filter.ticker
    algo = trade_filter.algo
    logging.info(ticker)
    logging.info(algo)
    trades_cursor = running_trade_collection.find({"symbol": ticker, "algo": algo})

    trades = []
    for trade_doc in trades_cursor:
        trade = Trade(
            contracts=trade_doc["contracts"],
            open_positions=trade_doc["open_positions"],
            order_status=trade_doc["order_status"],
            alert_type=trade_doc["alert_type"],
            symbol=trade_doc["symbol"],
            events=[
                Event(
                    signal=event["signal"],
                    status=event["status"],
                    side=event["side"],
                    contracts=event["contracts"],
                    price=float(event["price"])
                )
                for event in trade_doc["events"]
            ]
        )
        trades.append(trade)

    if not trades:
        raise HTTPException(status_code=400, detail="No trades found for the provided ticker")

    trades = calculate_profit_loss(trades)

    return {"ticker": ticker, "trades": trades}

@app.post("/create-user")
async def create_users(users: List[UserConfig]):
    results = []
    for user in users:
        # Create a query to find the user by username and trading account
        query = {"username": user.username, "trading_account": user.trading_account}
        
        # Find existing user config
        existing_user = user_configs_collection.find_one(query)
        
        # Define the update operation
        update = {"$set": user.dict()}
        
        # Perform the upsert operation
        result = user_configs_collection.update_one(query, update, upsert=True)
        
        # Check if a new document was inserted or an existing one was updated
        if result.upserted_id:
            message = "User created successfully"
            user_id = str(result.upserted_id)
        else:
            message = "User updated successfully"
            user_id = str(user_configs_collection.find_one(query)["_id"])

        # Check if any subscribed stock's is_active has changed to false
        if existing_user:
            for new_stock in user.subscribed_stocks:
                for existing_stock in existing_user['subscribed_stocks']:
                    if (existing_stock['symbol'] == new_stock.symbol and
                        existing_stock['is_active'] and not new_stock.is_active):
                        # Call place_order_for_user to trigger a trade close
                        user_running_trade = await get_user_running_trade(existing_user['_id'], existing_stock['interval'], existing_stock['algo'], existing_stock['symbol'])
                        if user_running_trade:
                            alert_item = AlertItem(symbol=new_stock.symbol, algo=new_stock.algo, interval=new_stock.interval, exchange=user_running_trade['exchange'], open='0', close='0', volume='0', alert_type=user_running_trade['alert_type'], signal="ST", source="api")  # Create an appropriate AlertItem instance
                            await place_order_for_user(alert_item, existing_user)
                        logging.info(f"Stock {new_stock.symbol} is inactive. Triggered order placement for user {user.username}.")
        
        results.append({"message": message, "user_id": user_id})
    
    return results

@app.post("/cancel-trade")
async def cancel_running_trade(alertItem: AlertItem):
    user_configs = fetch_user_configs()
    tasks = [asyncio.create_task(cancel_trade_for_user(alertItem, user)) for user in user_configs]
    await asyncio.gather(*tasks)
    return "Trade successfully closed"

@app.post("/subscribe-stock-to-user")
async def subscribe_user_to_stock(subscribedStock: SubscribedStock, users: List[str]):
    for username in users:
        # Create a query to find the user by username
        query = {"username": username}
        
        # Find existing user config
        existing_user = await user_configs_collection.find_one(query)
        
        if not existing_user:
            raise HTTPException(status_code=404, detail=f"User {username} not found")
        
        # Initialize a flag to check if the stock exists in subscribed_stocks
        stock_exists = False

        # Iterate over subscribed_stocks to check and update if the stock exists
        for stock in existing_user['subscribed_stocks']:
            if stock['symbol'] == subscribedStock.symbol:
                stock_exists = True
                if not stock['is_active']:
                    stock['is_active'] = True
                    await user_configs_collection.update_one(query, {"$set": {"subscribed_stocks": existing_user['subscribed_stocks']}})
                    logging.info(f"Stock {subscribedStock.symbol} activated for user {username}.")
                break
        
        # If the stock does not exist in subscribed_stocks, add it
        if not stock_exists:
            new_stock = {
                "symbol": subscribedStock.symbol,
                "is_active": subscribedStock,
                "investment_long": subscribedStock.investment_long,
                "investment_short": subscribedStock.investment_short,
                "algo": subscribedStock.algo,
                "type": subscribedStock.type
            }

            await user_configs_collection.update_one(query, {"$push": {"subscribed_stocks": new_stock}})
            logging.info(f"Stock {subscribedStock.symbol} added to subscribed stocks for user {username}.")
    
    return {"message": "Stock subscription processed successfully"}

@app.post("/place-order")
async def place_order(alertItem: AlertItem):
    logging.info("Placing order triggered: %s", alertItem)

    if alertItem.alert_type not in ["Long", "Short"]:
        return {"error": f"Invalid action: {alertItem.alert_type}"}
    
    if alertItem.signal not in ["Long", "Short", "TP1", "TP2", "TP3", "TP4", "SL", "ST"]:
        return {"error": f"Invalid action: {alertItem.signal}"}
    
    user_configs = fetch_user_configs()
    tasks = [asyncio.create_task(place_order_for_user(alertItem, user)) for user in user_configs]
    await asyncio.gather(*tasks)
    return "All Good"

@app.post("/scale-order")
async def scale_order_endpoint(alert_item: ScaleAlertItem):
    logging.info("Scale order triggered: %s", alert_item)

    if alert_item.alert_type not in ["Long", "Short"]:
        return {"error": f"Invalid action: {alert_item.alert_type}"}
    
    if alert_item.signal not in ["ScaleInLong", "ScaleInShort", "ScaleOutShort", "ScaleOutLong"]:
        return {"error": f"Invalid action: {alert_item.signal}"}
    
    user_configs = fetch_user_configs()
    tasks = [
        asyncio.create_task(place_order_for_user(alert_item, user)) 
        for user in user_configs if user["is_scaling_enabled"]
    ]
    
    await asyncio.gather(*tasks)
    return {"message": "All Good"}

async def cancel_trade_for_user(alertItem: AlertItem, user):
    user_trade = running_trade_collection.find_one({
            'user_id': user['_id'],
            'interval': alertItem.interval,
            'algo': alertItem.algo, 
            'symbol': alertItem.symbol,
            'exit.signal': 'Open',
            'order_status': { '$nin': ['order_canceled', "order_closed"] }
        })
    if user_trade:
        logging.info("Cancel trade completed: %s", user_trade)
        await place_order_to_ibkr(user_trade, alertItem, 0, user, True)

async def place_order_for_user(alert_item: AlertItem, user):
    logging.info(f"Placing order for user...")

    if user["is_outsideRTH_enabled"] == False and marketIsOutsideRTH():
        logging.info(f"Outside RTH not supported for this user")
        return
    
    user_stock_subscription = user_subscription_to_symbol(user, alert_item)

    try:

        user_trade = await get_user_running_trade(user['_id'], alert_item.interval, alert_item.algo, alert_item.symbol)
        position_to_propagate = 0

        if user_trade:
            logging.info(user_trade)
            logging.info("has a ongoing trade trade...")

            # It means trade is running with same same alert_type.
            if user_trade["alert_type"] == alert_item.alert_type and user_trade["alert_type"] == alert_item.signal:
                logging.info("trade already running with same alert side...")
                return

            user_trade = build_trade_events_from_alert(user_trade, alert_item)

            open_event = next((x for x in user_trade['events'] if x['status'] == 'open'), None)

            if (open_event["signal"] == "Short" or open_event["signal"] == "Long"):
                logging.info("trade switched from long to short or short to long...")
                # THis require 1. propagate running trade, 2. create a new oposite trade (long to short or shor to long), 3. place new order with previous unclosed quantity propagated
                if user_stock_subscription and user_stock_subscription["type"] == "LongShort":
                    position_to_propagate = abs(user_trade["open_positions"]) if user_trade["order_status"] != 'order_canceled' else 0
                    #handle_trade_switch(user_trade)
                    #close the running trade
                    await place_order_to_ibkr(user_trade, alert_item, 0, user, False)
                    #create a new trade with the alert
                    user_trade = await create_new_trade(alert_item, user)
        else:
            logging.info("User has no trade running. Creating new one...")
            user_trade = await create_new_trade(alert_item, user)

        if user_trade:
            await place_order_to_ibkr(user_trade, alert_item, position_to_propagate, user, True)
    except Exception as e:
        logging.error(f"Error placing order for user {user['_id']}: {e}")

async def get_user_running_trade(user_id, interval, algo, symbol):
    logging.info(user_id)

    logging.info(interval)
    logging.info(algo)
    logging.info(symbol)

    user_trade = running_trade_collection.find_one({
    'user_id': user_id,
    'interval': interval,
    'algo': algo, 
    'symbol': symbol,
    'exit.signal': 'Open',
    'order_status': { '$eq': "order_running" }
    })

    return user_trade

async def create_new_trade(alertItem, user):
    logging.info("Creating new trade...")
    if alertItem.signal in ['TP1', 'TP2', 'TP3', 'TP4', 'SL', 'ST']:
        logging.warning("Unsupported trade creation signal while creatuing new trade")
        return
    user_stock_subscription = user_subscription_to_symbol(user, alertItem)
    logging.info(user_stock_subscription)
    if not user_stock_subscription:
        logging.info("User is not subscribed to this symbol with the specified parameters")
        return None

    if alertItem.signal == "Short" and user["is_account_shortable"] == False:
        logging.info("User account is not shortable")
        return None

    if user_stock_subscription["type"] != "LongShort" and alertItem.alert_type != user_stock_subscription["type"]:
        logging.info(f"Unsupported action for this stock: {alertItem.alert_type}")
        return None
    
    investment_amount = get_stock_investment_amount(user_stock_subscription, alertItem.alert_type)
    
    result = running_trade_collection.insert_one({
        'user_id': user['_id'],
        'symbol': alertItem.symbol,
        'algo': alertItem.algo,
        'interval': alertItem.interval,
        'alerted_at': datetime.datetime.now(),
        'alert_type': alertItem.alert_type,
        'exchange': alertItem.exchange,
        'entry': {
            'type': 'Entry ' + alertItem.alert_type,
            'signal': alertItem.signal,
            'created_at': datetime.datetime.now(),
            'price': alertItem.close
        },
        'exit': {
            'type': 'Exit ' + alertItem.alert_type,
            'signal': 'Open',
            'price': 0
        },
        'events': [
            {
                'signal': alertItem.signal,
                'status': 'open',
                'side': 'BUY' if alertItem.alert_type == 'Long' else 'SELL',
                'contracts': abs(math.floor(float(investment_amount) / float(alertItem.close))),
                'price': alertItem.close
            }
        ],
        'contracts': math.floor(investment_amount / float(alertItem.close)),
        'scaled_contracts': 0,
        'open_positions': 0,
        'order_status': 'new'
    })
    logging.info("New trade created with:")
    return running_trade_collection.find_one({'_id': result.inserted_id})

def build_trade_events_from_alert(trade, alertItem):
    try:
        logging.info("Building trade events from alert...")
        profit_signal_names = ["TP1", "TP2", "TP3", 'TP4']
        next_signal_should_be = determine_next_signal(trade, alertItem, profit_signal_names)
        alertItem.signal = next_signal_should_be
        
        signal_handlers = {
            'TP1' : handle_tp1,
            'TP2' : handle_tp2,
            'TP3' : handle_tp3,
            'TP4' : handle_tp4,
            'SL' : handle_sl,
            'ST' : handle_st,
            'Short' : handle_short,
            'Long' : handle_long,
            'ScaleInLong' : handle_scale_in_long,
            'ScaleOutLong' : handle_scale_out_long,
            'ScaleInShort' : handle_scale_out_short,
            'ScaleOutShort' : handle_scale_out_short,
        }

        handler = signal_handlers.get(alertItem.signal)

        if handler:
            handler(trade, alertItem)
        else:
            logging.warning("No handler for signal: %s", alertItem.signal)
        
        '''running_trade_collection.update_one(
            {'_id': running_trade["_id"]},
            {'$set': {
                'events': running_trade["events"],
                "exit.signal": running_trade["exit"]["signal"]
            }}
        )'''

        logging.info("Running trade updated: %s", trade)
        return trade
    except Exception as e:
        logging.error("Error closing position: %s", e)

def user_subscription_to_symbol(user, alert_item):
    if not user["is_active"]:
      return None

    for stock in user['subscribed_stocks']:
        if not stock["is_active"]:
            continue

        if stock["symbol"] != alert_item.symbol or \
            stock["interval"] != alert_item.interval or \
            stock["algo"] != alert_item.algo:
            continue

        #if "algo" in stock and stock["algo"] != alert_item.algo:
            #continue

        return stock

    return None

def get_stock_investment_amount(user_subscription_stock, alert_type):
    if user_subscription_stock['type'] == 'Long':
        return float(user_subscription_stock['investment_long'])
    elif user_subscription_stock['type'] == 'Short':
        return float(user_subscription_stock['investment_long'])
    elif user_subscription_stock['type'] == 'LongShort':
        return float(user_subscription_stock['investment_long']) if alert_type == "Long" else float(user_subscription_stock['investment_short'])
        
    return 0.0

def determine_next_signal(running_trade, alertItem, profit_signal_names):
    next_signal_should_be = alertItem.signal
    if alertItem.signal in profit_signal_names:
        for signal_name in profit_signal_names:
            if signal_name not in {event.get('signal') for event in running_trade["events"]}:
                next_signal_should_be = signal_name
                break
        logging.info(f"Updated signal to '{next_signal_should_be}'")
    return next_signal_should_be
    
def handle_tp1(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side, alertItem, 'TP1', abs(math.floor(running_trade["contracts"] / 2)))

def handle_tp2(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side, alertItem, 'TP2',abs(math.floor(running_trade["open_positions"] / 2)))

def handle_tp3(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side,  alertItem, 'TP3', abs(math.floor(running_trade["open_positions"] / 2)))

def handle_tp4(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side,  alertItem, 'TP4', abs(running_trade["open_positions"]))
    running_trade["exit"]["signal"] = 'TP4'
    running_trade["exit"]["price"] = alertItem.close

def handle_sl(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side, alertItem, 'SL', abs(running_trade["open_positions"]))
    running_trade["exit"]["signal"] = 'SL'
    running_trade["exit"]["price"] = alertItem.close

def handle_st(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side, alertItem, 'ST', abs(running_trade["open_positions"]))
    running_trade["exit"]["signal"] = 'ST'
    running_trade["exit"]["price"] = alertItem.close

def handle_short(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side, alertItem, 'Short', abs(running_trade["open_positions"]))
    running_trade["exit"]["signal"] = 'Short'
    running_trade["exit"]["price"] = alertItem.close

def handle_long(running_trade, alertItem):
    side = 'SELL' if running_trade["alert_type"] == 'Long' else 'BUY'
    add_event(running_trade, side, alertItem, 'Long', abs(running_trade["open_positions"]))
    running_trade["exit"]["signal"] = 'Long'
    running_trade["exit"]["price"] = alertItem.close

def handle_scale_in_long(running_trade, alertItem):
    logging.info(getattr(alertItem, "scale_amount", 0))
    contract = abs(math.floor(float(getattr(alertItem, "scale_amount", 0)) / float(alertItem.close)))
    logging.info(contract)
    add_event(running_trade, "BUY", alertItem, 'ScaleInLong', contract)

def handle_scale_out_long(running_trade, alertItem):
    logging.info(getattr(alertItem, "scale_amount", None))
    contract = abs(math.floor(float(getattr(alertItem, "scale_amount", 0)) / float(alertItem.close)))
    add_event(running_trade, "SELL", alertItem, 'ScaleOutLong', contract)

def handle_scale_in_short(running_trade, alertItem):
    contract = abs(math.floor(float(getattr(alertItem, "scale_amount", 0)) / float(alertItem.close)))
    add_event(running_trade, "SALE", alertItem, 'ScaleInLShort', contract)

def handle_scale_out_short(running_trade, alertItem):
    contract = abs(math.floor(float(getattr(alertItem, "scale_amount", 0)) / float(alertItem.close)))
    add_event(running_trade, "BUY", alertItem, 'ScaleOutShort', contract)
 
def add_event(trade, side, alert_item, signal, contracts):
    # List of signals to skip
    skip_signals = ["ScaleInLong", "ScaleInShort", "ScaleOutShort", "ScaleOutLong"]
    
    # Check if the event already exists or if the signal is in the skip list
    for event in trade["events"]:
        if event["signal"] == signal and signal not in skip_signals:
            logging.info(f"Event {signal} already exists. Skipping.")
            return

    trade["events"].append({
        'signal': signal,
        'status': 'open',
        'side': side,
        'contracts': contracts,
        'price': alert_item.close
    })
    logging.info(f"Added event {signal} with {contracts} contracts at price {alert_item.close}")

def fetch_user_configs():
    return list(user_configs_collection.find())

async def place_order_to_ibkr(trade, alert_item, position_propagated: int, user, execute_order=True):

    logging.info(f'Placing order: {trade["symbol"]} user: {user["username"]}')

    event_to_process = next((x for x in trade['events'] if x['status'] == 'open'), None)
    side = event_to_process["side"]
    signal = event_to_process["signal"]
    contracts = event_to_process["contracts"]

    logging.info(user["host"])
    ibkr_rest_api = REST(user["host"] + ":" + str(user["port"]), ssl=False)
    
    outsideRTH = marketIsOutsideRTH()

    # Determine if current time is premarket or after-hours
    if (outsideRTH):
        order_type = "MKT"  # Limit order for premarket and after-hours
        outside_rth = False
        tif = "GTC"
    else:
        order_type = "MKT"  # Market order for regular market hours
        outside_rth = False
        tif = "GTC"
    
    try:
        if event_to_process:
            logging.info(user["is_tracking_only"])
            logging.info(outsideRTH)
            if outsideRTH == False and user["is_tracking_only"] == False:
            #if user["is_tracking_only"] == False:
                logging.info("placing order to ibkr")
                ibkt_list_of_orders = [
                {
                    "conid": ibkr_rest_api.get_conid(trade["symbol"]),
                    "orderType": order_type,
                    "side": side,
                    "quantity": contracts + position_propagated,
                    "tif": "GTC",
                    "outsideRTH": outside_rth
                }
                ]
        
                if execute_order:
                    logging.info("placing to ibkr.......")
                    logging.info(user["trading_account"])
                    ibkr_order_response = ibkr_rest_api.submit_orders(ibkt_list_of_orders, user["trading_account"])
                    event_to_process["order_id"] = ibkr_order_response["order_id"]
                    logging.info(ibkr_order_response)

            event_to_process["status"] = 'executed'

            if  user["is_tracking_only"] == True:
                 event_to_process["status"] = 'executed'

            if side == "SELL":
                trade["open_positions"] -= contracts
            else:
                trade["open_positions"] += contracts

            if position_propagated > 0:
                trade["position_propagated"] = position_propagated

            adjustScaleInOutSize(signal, trade, side, contracts)

            if trade["order_status"] == 'new':
                trade["order_status"] = 'order_running'

            if trade["open_positions"] == 0:
                trade["order_status"] = 'order_closed'
                trade["exit"]["signal"] = signal
                trade["exit"]["price"] = alert_item.close
                

            logging.info(trade)

            running_trade_collection.update_one(
            {'_id': trade["_id"]},
            {'$set': trade})
            logging.info(f'Order successfully processed: {trade["symbol"]}')

    except Exception as e:
        logging.info(f'Error while placing order: {trade["symbol"]}')
        logging.error(f'Error receieved from ibkr: {e}')
        event_to_process["error"] = str(e)
        event_to_process["status"] = 'failed'
        trade["order_status"] = 'order_failed'
        trade["comment"] = 'One or more event was failed. Please close manually'
        running_trade_collection.update_one(
            {'_id': trade["_id"]},
            {'$set': trade}
        )

        logging.info(f'Order failed to processed: {trade["symbol"]}')
        
        trade_failures_collection.insert_one({
            'user_id': user['_id'],
            'symbol': trade["symbol"],
            'algo': trade["algo"],
            'interval': trade["interval"],
            'alerted_at': datetime.datetime.now(),
            'alert_type': trade["alert_type"],
            'exchange': trade["exchange"],
            'error_message': str(e),
            'created_at': datetime.datetime.now()
        })

def marketIsOutsideRTH():
    # Determine current time
    tz = pytz.timezone('America/New_York')  # Nasdaq time zone
    now = datetime.datetime.now(tz)
    
    # Define premarket and after-hours times
    premarket_start = datetime.time(4, 0)  # 4:00 AM
    market_start = datetime.time(9, 30)  # 9:30 AM
    market_end = datetime.time(16, 0)  # 4:00 PM
    after_hours_end = datetime.time(20, 0)  # 8:00 PM
    outsideRTH = (premarket_start <= now.time() < market_start) or (market_end <= now.time() < after_hours_end)

    return outsideRTH


def adjustScaleInOutSize(signal, trade, side, scaled_contract):
     scale_signals = ["ScaleInLong", "ScaleInShort", "ScaleOutShort", "ScaleOutLong"]

     if side == "SELL" and signal in scale_signals:
         trade["scaled_contracts"] -= scaled_contract
     elif side == "BUY" and signal in scale_signals:
        trade["scaled_contracts"] +=scaled_contract
     

async def close_propagated_contract(trade, side, position_propagated: int, user, ibkr_rest_api):

    if position_propagated <= 0:
        return

    ibkt_list_of_orders = [
    {
        "conid": ibkr_rest_api.get_conid(trade["symbol"]),
        "orderType": "MKT",
        "side": side,
        "quantity": position_propagated,
            "tif": "GTC",
        }
    ]
        
    ibkr_rest_api.submit_orders(ibkt_list_of_orders, user["trading_account"])
    trade["propagated_position_status"] = "closed"
    running_trade_collection.update_one(
            {'_id': trade["_id"]},
            {'$set': {
                'propagated_position_status': "closed"
                }
            }
    )
    

def calculate_profit_loss(trades: List[Trade]) -> List[Trade]:
        for trade in trades:
            if trade.order_status == "order_failed" or trade.order_status == "order_running":
                continue

            events = trade.events

            buying_total = 0
            selling_total = 0
            profitOrLoss = 0
            profitOrLossPercentage = 0

            for event in events:
                signal = event.signal
                side = event.side
                contracts = event.contracts
                price = event.price

                if side == 'SELL':
                    selling_total += contracts * price

                elif side == 'BUY':
                    buying_total += contracts * price

            if trade.alert_type == "Short":
                profitOrLoss =  -(buying_total - selling_total)
                profitOrLossPercentage = (profitOrLoss * 100)/selling_total

            elif trade.alert_type == "Long":
                profitOrLoss =  -(buying_total - selling_total)
                profitOrLossPercentage = (profitOrLoss * 100)/buying_total

            trade.profitOrLoss = profitOrLoss
            trade.profitOrLossPercentage = profitOrLossPercentage


        return trades

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=80, reload=True)
