import os
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "attendance_db")

USERS_COLLECTION = "users"
TRACKS_COLLECTION = "tracks"

# ============================================================
# TRACK SUBJECTS
# ============================================================

AI_ML_SUBJECTS = [
    "SOFTWARE ENGINEERING",
    "CLOUD COMPUTING",
    "FULL STACK WEB DEVELOPMENT",
    "FULL STACK WEB DEVELOPMENT - PRACTICALS",
    "DEVSECOPS",
    "NOSQL DATABASE",
    "NOSQL DATABASE - PRACTICALS",
    "RESEARCH METHODOLOGY -I",
    "DEEP LEARNING",
    "DEEP LEARNING - PRACTICALS",
]

SECURITY_SUBJECTS = [
    "SOFTWARE ENGINEERING",
    "CLOUD COMPUTING",
    "FULL STACK WEB DEVELOPMENT",
    "FULL STACK WEB DEVELOPMENT - PRACTICALS",
    "DEVSECOPS",
    "NOSQL DATABASE",
    "NOSQL DATABASE - PRACTICALS",
    "RESEARCH METHODOLOGY -I",
    "NETWORK SECURITY",
    "NETWORK SECURITY - PRACTICALS",
]

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# MONGODB CONNECTION
# ============================================================

def connect_mongodb():
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI is missing. "
            "Please set it in your .env file."
        )

    try:
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000
        )

        client.admin.command("ping")

        logger.info("✓ Connected to MongoDB")

        return client

    except PyMongoError as e:
        logger.error(f"✗ MongoDB connection failed: {e}")
        raise


# ============================================================
# CREATE / UPDATE TRACK
# ============================================================

def create_or_update_track(db, track_name, subjects):
    tracks_collection = db[TRACKS_COLLECTION]

    result = tracks_collection.update_one(
        {
            "track": track_name
        },
        {
            "$set": {
                "track": track_name,
                "subjects": subjects
            }
        },
        upsert=True
    )

    if result.upserted_id:
        logger.info(f"✓ Created track: {track_name}")
    else:
        logger.info(f"✓ Updated track: {track_name}")

    logger.info(
        f"  {track_name}: {len(subjects)} subjects"
    )


# ============================================================
# MAKE ALL STUDENTS AI/ML
# ============================================================

def update_all_students_to_ai_ml(db):
    users_collection = db[USERS_COLLECTION]

    result = users_collection.update_many(
        {
            "status": "active"
        },
        {
            "$set": {
                "track": "AI/ML"
            }
        }
    )

    logger.info(
        f"✓ Set {result.modified_count} active student(s) "
        f"to AI/ML"
    )


# ============================================================
# DISPLAY TRACKS
# ============================================================

def show_tracks(db):
    tracks_collection = db[TRACKS_COLLECTION]

    print("\n" + "=" * 70)
    print("TRACK CONFIGURATION")
    print("=" * 70)

    for track in tracks_collection.find({}).sort("track", 1):

        print(f"\nTrack: {track['track']}")
        print("-" * 50)

        for index, subject in enumerate(
            track.get("subjects", []),
            start=1
        ):
            print(f"{index:2}. {subject}")

    print("=" * 70)


# ============================================================
# MAIN
# ============================================================

def main():

    client = None

    try:
        # ----------------------------------------------------
        # Connect
        # ----------------------------------------------------

        client = connect_mongodb()
        db = client[DB_NAME]

        # ----------------------------------------------------
        # 1. Make all active students AI/ML
        # ----------------------------------------------------

        update_all_students_to_ai_ml(db)

        # ----------------------------------------------------
        # 2. Create / update AI/ML track
        # ----------------------------------------------------

        create_or_update_track(
            db,
            "AI/ML",
            AI_ML_SUBJECTS
        )

        # ----------------------------------------------------
        # 3. Create / update SECURITY track
        # ----------------------------------------------------

        create_or_update_track(
            db,
            "SECURITY",
            SECURITY_SUBJECTS
        )

        # ----------------------------------------------------
        # 4. Show final configuration
        # ----------------------------------------------------

        show_tracks(db)

        logger.info("✓ Setup completed successfully")

    except Exception as e:
        logger.error(f"✗ Setup failed: {e}")

    finally:
        if client:
            client.close()
            logger.info("MongoDB connection closed")


if __name__ == "__main__":
    main()
