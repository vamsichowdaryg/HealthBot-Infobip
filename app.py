from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json
import os
import uvicorn # For running the FastAPI app

app = FastAPI()

DATA_FILE = "User.json"

# --- Data Loading and Saving ---
def load_users():
    """Loads user data from the JSON file."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    # If file doesn't exist, create it with an empty dictionary
    with open(DATA_FILE, "w") as f:
        json.dump({}, f)
    return {}

def save_users(data):
    """Saves user data to the JSON file."""
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# Load users at startup
users = load_users()

# --- Pydantic Models for Request Bodies ---

class SmartcardRequest(BaseModel):
    smartcardNumber: str

class MobileVerificationRequest(BaseModel):
    smartcardNumber: str
    phoneNumber: str

class MovieAddRequest(BaseModel):
    smartcardNumber: str
    movieTitle: str # Simplified to just title

class TopUpRequest(BaseModel):
    smartcardNumber: str
    amount: int

# --- API Endpoints ---

@app.get("/")
def root():
    """Root endpoint for basic service check."""
    return {"message": "Welcome to the FastAPI service!"}

@app.post("/verify-smartcard")
def verify_smartcard(request_data: SmartcardRequest):
    """
    Verifies if a smartcard number exists in the system.
    Expects smartcardNumber in the request body.
    """
    smartcard_number = request_data.smartcardNumber
    if smartcard_number in users:
        return {"valid": True, "message": "Smartcard verified. Please enter your mobile number."}
    raise HTTPException(status_code=404, detail="Smart Card Number not found")

@app.post("/verify-phone")
def verify_phone(request_data: MobileVerificationRequest):
    """
    Verifies if the provided phone number matches the smartcard number.
    Expects smartcardNumber and phoneNumber in the request body.
    """
    smartcard_number = request_data.smartcardNumber
    phone_number = request_data.phoneNumber
    
    user = users.get(smartcard_number)
    if user and user.get("phone") == phone_number:
        return {"valid": True, "message": "Phone number verified. You can now access services."}
    raise HTTPException(status_code=400, detail="Phone number does not match for given Smart Card")

@app.post("/add-movie")
def add_movie(request_data: MovieAddRequest):
    """
    Adds a movie to the user's list.
    Expects smartcardNumber and movieTitle in the request body.
    """
    smartcard_number = request_data.smartcardNumber
    movie_title = request_data.movieTitle

    user = users.get(smartcard_number)
    if not user:
        raise HTTPException(status_code=404, detail="Smart Card Number not found")
    
    if "movies" not in user:
        user["movies"] = [] # Initialize if not present
    
    if movie_title not in user["movies"]: # Prevent duplicates
        user["movies"].append(movie_title)
        save_users(users) # Save changes to file
        return {"message": f"Movie '{movie_title}' added successfully to {smartcard_number}", "movies": user["movies"]}
    else:
        return {"message": f"Movie '{movie_title}' is already in your list for {smartcard_number}", "movies": user["movies"]}


@app.get("/balance/{smartcard_number}")
def check_balance(smartcard_number: str):
    """
    Checks the balance and lists movies for a given smartcard number.
    Smartcard number is part of the URL path.
    """
    user = users.get(smartcard_number)
    if user:
        return {
            "smartcard_number": smartcard_number,
            "balance": user.get("balance", 0),
            "movies": user.get("movies", [])
        }
    raise HTTPException(status_code=404, detail="Smart Card Number not found")

@app.post("/top-up")
def top_up(request_data: TopUpRequest):
    """
    Adds amount to the user's balance.
    Expects smartcardNumber and amount in the request body.
    """
    smartcard_number = request_data.smartcardNumber
    amount = request_data.amount

    user = users.get(smartcard_number)
    if user:
        user["balance"] += amount
        save_users(users)  # Save to file
        return {"smartcard_number": smartcard_number, "new_balance": user["balance"]}
    raise HTTPException(status_code=404, detail="Smart Card Number not found")

@app.get("/all-users")
def get_all_users():
    """Returns all user data (for debugging/admin purposes)."""
    return users

# --- Run the application ---
if __name__ == "__main__":
    # For local development, use: uvicorn main:app --reload --port 8000
    # Replace 'main' with the name of your Python file (e.g., 'your_file_name:app')
    uvicorn.run(app, host="0.0.0.0", port=8000)
