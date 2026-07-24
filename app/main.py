"""hiiiiiii
Desription of file:
This code is a fully functional, lightweight REST API built using the FastAPI framework in Python.
It implements a basic CRUD (Create, Read, Update, Delete) system for managing an inventory of "items." It uses an in-memory Python dictionary (_store) as a temporary database, meaning any data you create will disappear when the application restarts. It also uses Pydantic to validate incoming data to ensure users are sending the correct types of information (like ensuring price and stock aren't negative).
"""

import os  # Imports the built-in os module to interact with the operating system and environment variables
import uuid  # Imports the uuid module to generate unique identifiers (UUIDs) for our items
from datetime import datetime, timezone  # Imports datetime and timezone to handle creation and update timestamps
from typing import Optional  # Imports Optional to mark certain fields as not strictly required

from fastapi import FastAPI, HTTPException, status  # Imports FastAPI (the framework), HTTPException (for error handling), and status (for standard HTTP status codes)
from pydantic import BaseModel, Field  # Imports Pydantic tools to define and validate data structures

app = FastAPI(title="FastAPI Demo", version=os.getenv("APP_VERSION", "1.0.0"))  # Initializes the FastAPI application, setting a title and fetching the version from environment variables (defaulting to "1.0.0")

INSTANCE_ID = os.getenv("INSTANCE_ID", "local")  # Retrieves the instance ID from environment variables, defaulting to "local" if not found
_store: dict[str, dict] = {}  # Creates an empty in-memory dictionary to act as our temporary database for items


class ItemIn(BaseModel):  # Defines a Pydantic model for data we expect when creating or fully replacing an item
    name: str = Field(min_length=1, max_length=100)  # Defines a 'name' string field that must be between 1 and 100 characters long
    price: float = Field(ge=0)  # Defines a 'price' float field that must be greater than or equal to (ge) 0
    stock: int = Field(ge=0)  # Defines a 'stock' integer field that must be greater than or equal to 0


class ItemPatch(BaseModel):  # Defines a Pydantic model for data we expect when partially updating an item
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)  # Optional 'name' field; if provided, must be 1-100 characters
    price: Optional[float] = Field(default=None, ge=0)  # Optional 'price' field; if provided, must be >= 0
    stock: Optional[int] = Field(default=None, ge=0)  # Optional 'stock' field; if provided, must be >= 0


@app.get("/")  # Defines a route that listens for HTTP GET requests at the root URL path ("/")
def root():  # Defines the function that runs when the root endpoint is accessed
    return {  # Returns a dictionary that FastAPI will automatically convert into a JSON response
        "status": "healthy",  # Indicates the app is running
        "instance_id": INSTANCE_ID,  # Returns the server instance ID we grabbed earlier
        "version": app.version,  # Returns the current version of the app
    }  # Closes the return dictionary


@app.get("/health")  # Defines a route for a slightly more detailed health check at "/health"
def health():  # Defines the function for the health endpoint
    return {  # Returns a JSON dictionary
        "status": "ok",  # Indicates general status is OK
        "checks": {"api": "ok", "store": "ok"},  # Provides a nested dictionary pretending to show the health of subsystems
        "instance_id": INSTANCE_ID,  # Returns the server instance ID
    }  # Closes the return dictionary


@app.post("/items", status_code=status.HTTP_201_CREATED)  # Defines a route to handle POST requests at "/items", returning a 201 (Created) status code on success
def create_item(item: ItemIn):  # Defines the function, expecting the body to match the 'ItemIn' Pydantic model we defined earlier
    item_id = str(uuid.uuid4())  # Generates a random, unique string ID for the new item
    now = datetime.now(timezone.utc).isoformat()  # Gets the current UTC time and formats it as an ISO 8601 string
    record = {**item.model_dump(), "id": item_id, "created_at": now, "updated_at": now}  # Converts the Pydantic item into a dictionary, injecting the generated ID and timestamps
    _store[item_id] = record  # Saves the newly created item record into our in-memory dictionary database under its ID key
    return record  # Returns the complete item record (including ID and timestamps) to the user


@app.get("/items")  # Defines a route to handle GET requests at "/items" to fetch all items
def list_items():  # Defines the function for listing items
    return list(_store.values())  # Extracts all the item dictionaries from our `_store` dictionary and returns them as a JSON array


@app.get("/items/{item_id}")  # Defines a route to handle GET requests at "/items/{item_id}" to fetch a specific item
def get_item(item_id: str):  # Defines the function, taking the 'item_id' from the URL path as a string argument
    if item_id not in _store:  # Checks if the requested ID is missing from our in-memory database
        raise HTTPException(status_code=404, detail="Item not found")  # If missing, halts execution and returns a 404 Not Found error to the user
    return _store[item_id]  # If found, returns the specific item's data dictionary


@app.put("/items/{item_id}")  # Defines a route to handle PUT requests to fully replace an existing item at the given ID
def replace_item(item_id: str, item: ItemIn):  # Takes the ID from the URL and expects a full 'ItemIn' payload in the request body
    if item_id not in _store:  # Checks if the item we want to replace actually exists
        raise HTTPException(status_code=404, detail="Item not found")  # Returns a 404 error if the item doesn't exist
    existing = _store[item_id]  # Retrieves the current version of the item from the database
    _store[item_id] = {  # Overwrites the database entry with a new dictionary
        **item.model_dump(),  # Unpacks the new data provided by the user
        "id": item_id,  # Ensures the ID remains the same
        "created_at": existing["created_at"],  # Preserves the original creation timestamp
        "updated_at": datetime.now(timezone.utc).isoformat(),  # Updates the 'updated_at' timestamp to right now
    }  # Closes the new dictionary
    return _store[item_id]  # Returns the newly replaced item data to the user


@app.patch("/items/{item_id}")  # Defines a route to handle PATCH requests for partially updating an item
def patch_item(item_id: str, patch: ItemPatch):  # Takes the ID from the URL and expects an 'ItemPatch' payload (where fields are optional)
    if item_id not in _store:  # Checks if the item exists
        raise HTTPException(status_code=404, detail="Item not found")  # Returns a 404 error if it doesn't
    updates = patch.model_dump(exclude_unset=True, exclude_none=True)  # Converts the incoming data to a dict, explicitly ignoring fields the user didn't include or set to None
    _store[item_id].update(updates)  # Updates only the provided fields in the existing dictionary entry
    _store[item_id]["updated_at"] = datetime.now(timezone.utc).isoformat()  # Refreshes the 'updated_at' timestamp
    return _store[item_id]  # Returns the partially updated item data


@app.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)  # Defines a route to handle DELETE requests, returning a 204 (No Content) success status
def delete_item(item_id: str):  # Takes the item ID from the URL
    if item_id not in _store:  # Checks if the item exists
        raise HTTPException(status_code=404, detail="Item not found")  # Returns a 404 error if the item doesn't exist
    del _store[item_id]  # Completely removes the item key and its data from our in-memory dictionary
    return None  # Returns nothing, which combined with the 204 status code, tells the user the deletion succeeded
