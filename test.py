import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

uri = os.getenv("MONGODB_URI")
db_name = os.getenv("DB_NAME", "attendance_db")

client = MongoClient(uri, serverSelectionTimeoutMS=5000)

try:
    client.admin.command("ping")
    print("✓ Connected to MongoDB Atlas")
except Exception as e:
    print(f"✗ Connection failed: {e}")
