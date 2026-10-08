import os
import re
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

BASE_URL = "https://attendence-system-1910.vercel.app"

LOGIN_URL = f"{BASE_URL}/api/auth/login"
DETAILS_URL = f"{BASE_URL}/api/attendances/details"
ATTENDANCE_URL = f"{BASE_URL}/api/students/attendances/details"

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "attendance_db")

USERS_COLLECTION = "users"
TRACKS_COLLECTION = "tracks"
RESULTS_COLLECTION = "attendance_results"

# Number of users processed concurrently.
# Start with 5. Increase slowly if the attendance server handles it.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

# If these are None, the first matching item from /api/attendances/details
# is selected. If you have multiple courses/batches/divisions/semesters,
# set the exact names here.
COURSE = None
BATCH = None
DIVISION = None
SEMESTER = None


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# HTTP SESSION
# ============================================================

def build_session():
    """
    One requests.Session per student.

    The session automatically stores the `token` cookie returned
    by /api/auth/login and sends it on later API requests.
    """
    session = requests.Session()

    session.headers.update({
        # Match the browser requests captured from the attendance website.
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
    })

    return session


def login(session, email, password):
    """
    Login and let requests.Session store the authentication cookie.
    """

    response = session.post(
        LOGIN_URL,
        json={
            "email": email,
            "password": password,
        },
        headers={
            "Referer": f"{BASE_URL}/users/login",
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    # Do NOT print the token itself. Only verify that the cookie exists.
    token = session.cookies.get("token")

    logger.info(
        "  Login response: %s | token cookie: %s",
        response.status_code,
        "YES" if token else "NO",
    )

    if not token:
        raise RuntimeError(
            f"Login returned {response.status_code}, "
            "but no 'token' cookie was received. "
            f"Cookies seen: {list(session.cookies.keys())}"
        )

    # The frontend sends the JWT in BOTH the token cookie and the
    # Authorization header. The API checks the Authorization header.
    session.headers.update({
        "Authorization": f"Bearer {token}",
    })

    logger.info("  ✓ Login successful for %s", email)


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_subject_name(subject):
    if not subject:
        return ""

    subject = str(subject).strip()
    subject = re.sub(r"\s+", " ", subject)

    return subject.upper()


def get_subjects_for_track(db, track):
    """
    Read allowed subjects from MongoDB.
    """

    if not track:
        return []

    document = db[TRACKS_COLLECTION].find_one({
        "track": track
    })

    if not document:
        logger.error("No track configuration found for: %s", track)
        return []

    subjects = document.get("subjects", [])

    if not isinstance(subjects, list):
        return []

    return [
        subject.strip()
        for subject in subjects
        if isinstance(subject, str) and subject.strip()
    ]


def filter_subjects_by_track(website_subjects, allowed_subjects):
    allowed = {
        normalize_subject_name(subject): subject
        for subject in allowed_subjects
    }

    return [
        subject
        for subject in website_subjects
        if normalize_subject_name(subject["name"]) in allowed
    ]


# ============================================================
# ATTENDANCE API
# ============================================================

def get_attendance_details(session):
    """
    GET the complete course/batch/division/semester/subject tree.
    """

    response = session.get(
        DETAILS_URL,
        headers={
            "Referer": f"{BASE_URL}/students/current/attendances",
            "Authorization": session.headers.get("Authorization", ""),
        },
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 403:
        # Useful diagnostic without exposing the authentication token.
        logger.error(
            "  403 from attendance details. Session cookies: %s | Body: %s",
            list(session.cookies.keys()),
            response.text[:500],
        )

    response.raise_for_status()

    data = response.json()

    if "attendanceDetails" not in data:
        raise RuntimeError(
            "Unexpected response from /api/attendances/details"
        )

    return data["attendanceDetails"]


def choose_by_name(items, wanted_name, label):
    """
    Select an item by name.

    If wanted_name is None, first item is used.
    """

    if not items:
        raise RuntimeError(f"No {label} found in attendance details.")

    if wanted_name is None:
        return items[0]

    wanted = normalize_subject_name(wanted_name)

    for item in items:
        if normalize_subject_name(item.get("name")) == wanted:
            return item

    available = [item.get("name") for item in items]

    raise RuntimeError(
        f"{label} '{wanted_name}' not found. "
        f"Available: {available}"
    )


def get_hierarchy(session):
    """
    Convert the nested attendanceDetails response into the IDs
    required by POST /api/students/attendances/details.
    """

    courses = get_attendance_details(session)

    course = choose_by_name(
        courses,
        COURSE,
        "course",
    )

    batch = choose_by_name(
        course.get("batches", []),
        BATCH,
        "batch",
    )

    division = choose_by_name(
        batch.get("divisions", []),
        DIVISION,
        "division",
    )

    semester = choose_by_name(
        division.get("semesters", []),
        SEMESTER,
        "semester",
    )

    subjects = []

    for semester_subject in semester.get("subjects", []):
        subject = semester_subject.get("subject")

        if not subject:
            continue

        subjects.append({
            "semester_subject_id": semester_subject.get("_id"),
            "subject_id": subject.get("_id"),
            "name": subject.get("name"),
        })

    return {
        "course": course,
        "batch": batch,
        "division": division,
        "semester": semester,
        "subjects": subjects,
    }


def get_one_subject_attendance(session, hierarchy, subject):
    """
    Direct replacement for the Selenium:
        select subject
        click View Attendance
        read attendance page
    """

    payload = {
        "batch": hierarchy["batch"]["_id"],
        "course": hierarchy["course"]["_id"],
        "division": hierarchy["division"]["_id"],
        "semester": hierarchy["semester"]["_id"],
        "semesterSubject": subject["semester_subject_id"],
        "subject": subject["subject_id"],
    }

    response = session.post(
        ATTENDANCE_URL,
        json=payload,
        headers={
            "Referer": f"{BASE_URL}/students/current/attendances",
            "Authorization": session.headers.get("Authorization", ""),
        },
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 403:
        logger.error(
            "  403 from attendance API for %s. Session cookies: %s | Body: %s",
            subject["name"],
            list(session.cookies.keys()),
            response.text[:500],
        )

    response.raise_for_status()

    data = response.json()

    required = [
        "total",
        "totalPresent",
        "percentage",
    ]

    if not all(key in data for key in required):
        raise RuntimeError(
            f"Unexpected attendance response for "
            f"{subject['name']}: {data}"
        )

    return {
        "subject": subject["name"],
        "total_attendances": int(data["total"]),
        "total_present": int(data["totalPresent"]),
        "percentage": float(data["percentage"]),
    }


# ============================================================
# STATISTICS
# ============================================================

def clean_rows(rows):
    cleaned = []

    for row in rows:
        total = row["total_attendances"]
        present = row["total_present"]

        # Same behavior as your Selenium scraper:
        # ignore subjects with zero attendance.
        if total == 0:
            continue

        cleaned.append({
            "subject": row["subject"],
            "total_attendances": total,
            "total_present": present,
            "percentage": row["percentage"],
        })

    return cleaned


def calculate_stats(rows):
    if not rows:
        return None

    total_attendances = sum(
        row["total_attendances"]
        for row in rows
    )

    total_present = sum(
        row["total_present"]
        for row in rows
    )

    overall_percentage = (
        total_present / total_attendances * 100
        if total_attendances
        else 0
    )

    average_percentage = (
        sum(row["percentage"] for row in rows)
        / len(rows)
    )

    return {
        "total_attendances": total_attendances,
        "total_present": total_present,
        "overall_percentage": round(
            overall_percentage,
            2,
        ),
        "avg_percentage": round(
            average_percentage,
            2,
        ),
        "subjects_count": len(rows),
    }


# ============================================================
# MONGODB
# ============================================================

def save_to_mongodb(
    db,
    username,
    email,
    track,
    rows,
    stats,
):
    document = {
        "username": username,
        "email": email,
        "track": track,
        "scraped_at": datetime.now(timezone.utc),
        "subjects": rows,
        "statistics": stats,
        "total_subjects_scraped": len(rows),
    }

    db[RESULTS_COLLECTION].update_one(
        {"email": email},
        {"$set": document},
        upsert=True,
    )


# ============================================================
# ONE USER
# ============================================================

def scrape_user(user, db):
    email = user.get("email")
    password = user.get("password")
    track = user.get("track")

    username = (
        email.split("@")[0]
        if email
        else "unknown"
    )

    logger.info(
        "USER START: %s | Track: %s",
        email,
        track,
    )

    if not email or not password:
        raise RuntimeError(
            f"{username}: missing email/password"
        )

    if not track:
        raise RuntimeError(
            f"{username}: missing track"
        )

    session = build_session()

    try:
        # 1. Login
        login(
            session,
            email,
            password,
        )

        # 2. Get course/batch/division/semester/subjects
        hierarchy = get_hierarchy(session)

        logger.info(
            "  Course: %s",
            hierarchy["course"]["name"],
        )
        logger.info(
            "  Batch: %s",
            hierarchy["batch"]["name"],
        )
        logger.info(
            "  Division: %s",
            hierarchy["division"]["name"],
        )
        logger.info(
            "  Semester: %s",
            hierarchy["semester"]["name"],
        )

        # 3. Track filter
        allowed_subjects = get_subjects_for_track(
            db,
            track,
        )

        if not allowed_subjects:
            raise RuntimeError(
                f"No subjects configured for track {track}"
            )

        subjects = filter_subjects_by_track(
            hierarchy["subjects"],
            allowed_subjects,
        )

        if not subjects:
            raise RuntimeError(
                f"No matching subjects for {username}"
            )

        logger.info(
            "  Subjects to scrape: %d",
            len(subjects),
        )

        # 4. Direct API calls
        rows = []

        for subject in subjects:
            logger.info(
                "  → %s",
                subject["name"],
            )

            try:
                row = get_one_subject_attendance(
                    session,
                    hierarchy,
                    subject,
                )

                rows.append(row)

            except Exception as exc:
                logger.warning(
                    "  ⚠ Failed %s: %s",
                    subject["name"],
                    exc,
                )

        # 5. Clean
        rows = clean_rows(rows)

        if not rows:
            raise RuntimeError(
                f"No attendance data collected for {username}"
            )

        # 6. Statistics
        stats = calculate_stats(rows)

        # 7. Save
        save_to_mongodb(
            db,
            username,
            email,
            track,
            rows,
            stats,
        )

        logger.info(
            "✓ USER COMPLETE: %s | %.2f%%",
            email,
            stats["overall_percentage"],
        )

        return True

    finally:
        session.close()


# ============================================================
# MAIN
# ============================================================

def main():
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI is not configured."
        )

    client = MongoClient(
        MONGODB_URI,
        serverSelectionTimeoutMS=5000,
    )

    db = client[DB_NAME]

    try:
        users = list(
            db[USERS_COLLECTION].find({
                "status": "active"
            })
        )

        if not users:
            logger.warning("No active users found.")
            return

        logger.info(
            "Found %d active users.",
            len(users),
        )

        logger.info(
            "Starting with %d concurrent workers.",
            MAX_WORKERS,
        )

        successful = 0
        failed = 0

        # Each worker receives a separate user/session.
        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futures = {
                executor.submit(
                    scrape_user,
                    user,
                    db,
                ): user
                for user in users
            }

            for future in as_completed(futures):
                user = futures[future]
                email = user.get(
                    "email",
                    "<unknown>",
                )

                try:
                    if future.result():
                        successful += 1
                    else:
                        failed += 1

                except Exception as exc:
                    failed += 1

                    logger.error(
                        "✗ USER FAILED: %s | %s",
                        email,
                        exc,
                    )

        logger.info("=" * 60)
        logger.info("SCRAPING COMPLETE")
        logger.info("Successful: %d", successful)
        logger.info("Failed: %d", failed)
        logger.info(
            "Total: %d",
            successful + failed,
        )
        logger.info("=" * 60)

    finally:
        client.close()


if __name__ == "__main__":
    main()
