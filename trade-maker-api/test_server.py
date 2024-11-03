from ib_api import REST
import pytest
import mongomock
from fastapi.testclient import TestClient
from api.server import app, fetch_user_configs, place_order_for_user, create_trade, user_configs_collection, running_trade_collection, trade_failures_collection

# Create a TestClient for the FastAPI app
client = TestClient(app)

# Mocking MongoDB collections
client_mongo = mongomock.MongoClient()
db = client_mongo['project_zero']
user_configs_collection = db['user_configs']
running_trade_collection = db['running_trades']
trade_failures_collection = db['trade_failures']

# Test data
test_user_config = {
    "_id": "user1",
    "host": "127.0.0.1",
    "port": "7497",
    "trading_account": "test_account"
}
user_configs_collection.insert_one(test_user_config)

test_alert_item = {
    "algo": "test_algo",
    "symbol": "AAPL",
    "alert_type": "Long",
    "signal": "Long",
    "interval": "1m",
    "investAmount": 1000.0,
    "exchange": "NASDAQ",
    "source": "test_source",
    "close": "150.0"
}

# Test cases
@pytest.mark.asyncio
async def test_read_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"Hello": "YES"}

@pytest.mark.asyncio
async def test_place_order_invalid_alert_type():
    invalid_alert_item = test_alert_item.copy()
    invalid_alert_item["alert_type"] = "Invalid"
    response = client.post("/place-order", json=invalid_alert_item)
    assert response.status_code == 200
    assert response.json() == {"error": "Invalid action: Invalid"}

@pytest.mark.asyncio
async def test_place_order_invalid_signal():
    invalid_alert_item = test_alert_item.copy()
    invalid_alert_item["signal"] = "Invalid"
    response = client.post("/place-order", json=invalid_alert_item)
    assert response.status_code == 200
    assert response.json() == {"error": "Invalid action: Invalid"}

@pytest.mark.asyncio
async def test_place_order_valid():
    response = client.post("/place-order", json=test_alert_item)
    assert response.status_code == 200
    assert response.text == "\"All Good\""
    running_trade = running_trade_collection.find_one({
        'user_id': test_user_config["_id"],
        'symbol': test_alert_item["symbol"]
    })
    assert running_trade is not None

@pytest.mark.asyncio
async def test_cancel_trade():
    response = client.post("/cancel-trade", json=test_alert_item)
    assert response.status_code == 200
    assert response.text == "\"All Good\""

@pytest.mark.asyncio
async def test_order_failure_handling(monkeypatch):
    async def mock_place_order_for_user(alertItem, user):
        raise Exception("Simulated order placement failure")

    monkeypatch.setattr('server.place_order_for_user', mock_place_order_for_user)
    response = await place_order_for_user(test_alert_item, test_user_config)
    assert response is None
    failure_log = trade_failures_collection.find_one({'user_id': test_user_config["_id"]})
    assert failure_log is not None
    assert failure_log["error_message"] == "Simulated order placement failure"
