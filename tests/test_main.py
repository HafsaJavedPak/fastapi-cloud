"""
**Description**:
This code is a test suite built using the pytest framework. It uses FastAPI's built-in TestClient to simulate network requests to the API we looked at previously, ensuring every endpoint behaves correctly without actually having to start up a live server.

**Usage**:
`pytest tests/test_main.py -v`
"""

import pytest  # Imports the pytest testing framework, which handles running the tests and reporting results
from fastapi.testclient import TestClient  # Imports FastAPI's tool for making fake HTTP requests to the app during tests
from app.main import app, _store  # Imports the FastAPI 'app' instance and our in-memory dictionary database ('_store') from the main code

client = TestClient(app)  # Creates a testing client connected to our app, which we will use to send GET, POST, PUT, etc. requests


@pytest.fixture(autouse=True)  # Tells pytest that this function is a "fixture" that must run automatically before and after EVERY test
def clear_store():  # Defines the setup/teardown function
    _store.clear()  # SETUP: Empties out the in-memory database before a test starts so each test gets a clean slate
    yield  # Pauses the fixture here and hands control over to the actual test function to run
    _store.clear()  # TEARDOWN: Empties the database again after the test finishes to clean up


def make_item(**overrides):  # A helper function to quickly generate test data; accepts optional keyword arguments to override defaults
    return {"name": "Widget", "price": 9.99, "stock": 5, **overrides}  # Returns a default valid dictionary, merging in any custom values provided in 'overrides'


def test_app01_root_healthy():  # Defines a test for the root ("/") endpoint
    r = client.get("/")  # Simulates an HTTP GET request to "/" and stores the response in 'r'
    assert r.status_code == 200  # Checks that the HTTP status code is 200 (OK)
    assert r.json()["status"] == "healthy"  # Parses the response body as JSON and checks that the "status" key equals "healthy"


def test_app02_health_checks_map():  # Defines a test for the "/health" endpoint
    r = client.get("/health")  # Simulates a GET request to "/health"
    assert r.status_code == 200  # Verifies the request was successful
    assert r.json()["checks"]["api"] == "ok"  # Verifies the nested "api" check inside "checks" returns "ok"


def test_app03_create_returns_201_and_uuid():  # Tests the item creation process
    r = client.post("/items", json=make_item())  # Sends a POST request to "/items" with valid mock data as the JSON payload
    assert r.status_code == 201  # Verifies the server responds with 201 (Created)
    assert len(r.json()["id"]) == 36  # Verifies the generated UUID string is exactly 36 characters long


def test_app04_missing_price_422():  # Tests validation when required data is missing
    r = client.post("/items", json={"name": "Widget", "stock": 5})  # Sends a POST request deliberately omitting the required 'price' field
    assert r.status_code == 422  # Verifies FastAPI automatically rejects it with a 422 (Unprocessable Entity) error


def test_app05_empty_list():  # Tests fetching all items when the database is empty
    r = client.get("/items")  # Sends a GET request to fetch the items list
    assert r.status_code == 200 and r.json() == []  # Verifies it succeeds and returns an empty JSON array


def test_app06_get_by_id():  # Tests fetching a specific item by its ID
    item_id = client.post("/items", json=make_item()).json()["id"]  # First, creates a new item and extracts its generated ID
    r = client.get(f"/items/{item_id}")  # Uses that ID to send a GET request for that specific item
    assert r.status_code == 200 and r.json()["name"] == "Widget"  # Verifies the request succeeds and the item name matches our mock data


def test_app07_ghost_404():  # Tests requesting an item that doesn't exist
    assert client.get("/items/ghost").status_code == 404  # Sends a request for an ID called "ghost" and verifies it returns a 404 (Not Found)


def test_app08_put_replaces():  # Tests fully replacing an item via PUT
    item_id = client.post("/items", json=make_item()).json()["id"]  # Creates an item to get a valid ID
    r = client.put(f"/items/{item_id}", json=make_item(name="New", price=1.0, stock=1))  # Sends a PUT request to overwrite it with entirely new data
    assert r.status_code == 200 and r.json()["name"] == "New"  # Verifies success and that the name was successfully changed to "New"


def test_app09_put_preserves_created_at():  # Tests that replacing an item doesn't overwrite its original creation timestamp
    created = client.post("/items", json=make_item()).json()  # Creates a new item and stores the full response
    r = client.put(f"/items/{created['id']}", json=make_item(name="New"))  # Updates the item
    assert r.json()["created_at"] == created["created_at"]  # Compares the new 'created_at' to the original to ensure they are identical


def test_app10_patch_partial():  # Tests partially updating an item via PATCH
    created = client.post("/items", json=make_item()).json()  # Creates a base item
    r = client.patch(f"/items/{created['id']}", json={"price": 42.0})  # Sends a PATCH request updating ONLY the price
    assert r.json()["price"] == 42.0 and r.json()["name"] == "Widget"  # Verifies the price changed, but the original name remained untouched


def test_app11_delete_204():  # Tests deleting an item
    item_id = client.post("/items", json=make_item()).json()["id"]  # Creates an item to delete
    assert client.delete(f"/items/{item_id}").status_code == 204  # Sends a DELETE request and verifies it returns a 204 (No Content) success status


def test_app12_delete_then_get_404():  # Tests that a deleted item is actually gone
    item_id = client.post("/items", json=make_item()).json()["id"]  # Creates an item
    client.delete(f"/items/{item_id}")  # Deletes it
    assert client.get(f"/items/{item_id}").status_code == 404  # Tries to fetch the deleted item and verifies it correctly fails with a 404


def test_app13_negative_price_422():  # Tests data validation for invalid prices
    assert client.post("/items", json=make_item(price=-1)).status_code == 422  # Attempts to create an item with a price of -1 and verifies it is blocked (422 error)


def test_app14_negative_stock_422():  # Tests data validation for invalid stock amounts
    assert client.post("/items", json=make_item(stock=-1)).status_code == 422  # Attempts to create an item with negative stock and verifies it is blocked
