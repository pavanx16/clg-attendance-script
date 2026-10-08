"""
Attendance System Scraper with Track-Based Subject Filtering

Workflow:

MongoDB users
    |
    | track = "AI/ML" / "SECURITY"
    v
MongoDB tracks
    |
    | allowed subjects
    v
Attendance Website
    |
    | available subjects
    v
FILTER
    |
    | only subjects belonging to student's track
    v
Scrape Attendance
    |
    v
MongoDB attendance_results
"""

import os
import re
import sys
import time
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
)

from webdriver_manager.chrome import ChromeDriverManager

from pymongo import MongoClient
from pymongo.errors import PyMongoError


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://attendence-system-1910.vercel.app"
LOGIN_URL = f"{BASE_URL}/users/login"

HEADLESS = True
WAIT_TIMEOUT = 20

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "attendance_db")

USERS_COLLECTION = "users"
TRACKS_COLLECTION = "tracks"
ATTENDANCE_RESULTS_COLLECTION = "attendance_results"

# Optional website filters
COURSE = None
BATCH = None
DIVISION = None
SEMESTER = None


# ============================================================
# MONGODB
# ============================================================

def connect_mongodb():
    """
    Connect to MongoDB Atlas.
    """

    if not MONGODB_URI:
        raise RuntimeError(
            "Missing MONGODB_URI. "
            "Set it in your .env file."
        )

    try:

        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000
        )

        # Verify connection
        client.admin.command("ping")

        logger.info("✓ Connected to MongoDB Atlas")

        return client

    except PyMongoError as e:

        logger.error(
            f"✗ MongoDB connection failed: {e}"
        )

        raise


# ============================================================
# USERS
# ============================================================

def get_users_from_mongodb(db):
    """
    Fetch all active users from MongoDB.
    """

    users_collection = db[USERS_COLLECTION]

    users = list(
        users_collection.find({
            "status": "active"
        })
    )

    if not users:

        logger.warning(
            "No active users found in MongoDB"
        )

        return []

    logger.info(
        f"Found {len(users)} active user(s)"
    )

    return users


def extract_username_from_email(email):
    """
    Extract username from email.
    """

    if not email:
        return "unknown"

    return email.split("@")[0]


# ============================================================
# TRACK / SUBJECT CONFIGURATION
# ============================================================

def normalize_subject_name(subject):
    """
    Normalize subject name so small differences in
    capitalization and spacing do not break matching.

    Example:

        "Network Security"
        "NETWORK SECURITY"
        "  NETWORK SECURITY  "

    all become:

        "NETWORK SECURITY"
    """

    if not subject:
        return ""

    subject = str(subject)

    # Remove leading/trailing spaces
    subject = subject.strip()

    # Convert multiple spaces to one
    subject = re.sub(r"\s+", " ", subject)

    # Case-insensitive comparison
    return subject.upper()


def get_subjects_for_track(db, track):
    """
    Get allowed subjects for a student's track.

    Example:

        track = "SECURITY"

    returns:

        [
            "SOFTWARE ENGINEERING",
            ...
            "NETWORK SECURITY",
            "NETWORK SECURITY - PRACTICALS"
        ]
    """

    tracks_collection = db[TRACKS_COLLECTION]

    if not track:

        logger.error(
            "Student has no track assigned"
        )

        return []

    track_document = tracks_collection.find_one({
        "track": track
    })

    if not track_document:

        logger.error(
            f"No track configuration found for: {track}"
        )

        return []

    subjects = track_document.get(
        "subjects",
        []
    )

    if not isinstance(subjects, list):

        logger.error(
            f"Invalid subjects configuration "
            f"for track: {track}"
        )

        return []

    subjects = [
        subject
        for subject in subjects
        if subject and isinstance(subject, str)
    ]

    logger.info(
        f"  Track '{track}' has "
        f"{len(subjects)} allowed subject(s)"
    )

    return subjects


def filter_subjects_by_track(
    website_subjects,
    allowed_subjects
):
    """
    Filter website subjects using the student's track.

    Only subjects existing in BOTH lists are returned.

    This is the most important protection against
    scraping subjects from another track.
    """

    allowed_subjects_map = {
        normalize_subject_name(subject): subject
        for subject in allowed_subjects
    }

    filtered_subjects = []

    for website_subject in website_subjects:

        normalized = normalize_subject_name(
            website_subject
        )

        if normalized in allowed_subjects_map:

            filtered_subjects.append(
                website_subject
            )

    return filtered_subjects


# ============================================================
# SELENIUM
# ============================================================

def build_driver():
    """
    Build and return Selenium WebDriver.
    """

    options = Options()

    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--incognito")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")

    service = Service(
        ChromeDriverManager().install()
    )

    return webdriver.Chrome(
        service=service,
        options=options
    )


def xpath_quote(value):
    """
    Safely quote a string for XPath.
    """

    if "'" not in value:
        return f"'{value}'"

    parts = value.split("'")

    return (
        "concat("
        + ", \"'\", ".join(
            f"'{part}'"
            for part in parts
        )
        + ")"
    )


def find_button(driver, text):
    """
    Find button by exact visible text.
    """

    return driver.find_element(
        By.XPATH,
        f"//button[normalize-space()={xpath_quote(text)}]"
    )


def safe_click(driver, element):
    """
    Click element safely.
    """

    try:

        driver.execute_script(
            "arguments[0].scrollIntoView(true);",
            element
        )

        element.click()

    except ElementClickInterceptedException:

        driver.execute_script(
            "arguments[0].click();",
            element
        )


# ============================================================
# DROPDOWNS
# ============================================================

def set_dropdown_if_specified(
    driver,
    wait,
    label_text,
    value
):
    """
    Set a React dropdown if a value is configured.

    If value is None, dropdown is left unchanged.
    """

    if value is None:
        return

    xpath_label = (
        f"//*[contains(text(), "
        f"{xpath_quote(label_text)})]"
    )

    container = wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                f"{xpath_label}/following-sibling::div "
                f"| "
                f"{xpath_label}/parent::*/following-sibling::div"
            )
        )
    )

    safe_click(
        driver,
        container
    )

    time.sleep(0.5)

    option = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                f"//*[normalize-space()="
                f"{xpath_quote(value)} "
                f"and not("
                f"ancestor-or-self::*"
                f"[@aria-hidden='true']"
                f")]"
            )
        )
    )

    safe_click(
        driver,
        option
    )

    time.sleep(0.6)


def get_subject_names(driver, wait):
    """
    Get all subjects available on the website.

    IMPORTANT:
    This function only discovers website subjects.
    It does NOT decide which subjects the student should
    scrape. Track filtering happens separately.
    """

    xpath_label = (
        "//*[contains(text(), 'Select Subjects')]"
    )

    container = wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                f"{xpath_label}/following-sibling::div "
                f"| "
                f"{xpath_label}/parent::*/following-sibling::div"
            )
        )
    )

    safe_click(
        driver,
        container
    )

    time.sleep(1)

    options = driver.find_elements(
        By.XPATH,
        "//li | //*[@role='option']"
    )

    subjects = []

    for option in options:

        try:

            text = option.text.strip()

            if not text:
                continue

            if text.lower() == "none":
                continue

            if text not in subjects:
                subjects.append(text)

        except StaleElementReferenceException:

            continue

    # Close dropdown
    safe_click(
        driver,
        container
    )

    time.sleep(0.5)

    return subjects


# ============================================================
# LOGIN
# ============================================================

def login(
    driver,
    wait,
    email,
    password
):
    """
    Login to attendance website.
    """

    driver.get(LOGIN_URL)

    email_input = wait.until(
        EC.presence_of_element_located(
            (
                By.CSS_SELECTOR,
                "input[placeholder='john@gmail.com'], "
                "input[type='email']"
            )
        )
    )

    password_input = driver.find_element(
        By.CSS_SELECTOR,
        "input[type='password']"
    )

    email_input.clear()
    email_input.send_keys(email)

    password_input.clear()
    password_input.send_keys(password)

    find_button(
        driver,
        "Log In"
    ).click()

    try:

        wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//button[normalize-space()='Your Attendances']"
                )
            )
        )

        logger.info(
            f"  ✓ Logged in as {email}"
        )

    except TimeoutException:

        raise RuntimeError(
            f"Login failed for {email}. "
            f"Check credentials or site status."
        )


# ============================================================
# ATTENDANCE PAGE
# ============================================================

def open_subject_selector(
    driver,
    wait
):
    """
    Open attendance subject selector.
    """

    find_button(
        driver,
        "Your Attendances"
    ).click()

    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//*[contains("
                "text(),"
                "'Select Subject For Attendance'"
                ")]"
            )
        )
    )

    time.sleep(3)


# ============================================================
# ATTENDANCE EXTRACTION
# ============================================================

def extract_summary(body_text):
    """
    Extract attendance summary from page text.
    """

    patterns = {

        "Subject":
            r"Subject:\s*(.+)",

        "Total Attendances":
            r"Total Attendances:\s*([a-zA-Z0-9]+)",

        "Total Present":
            r"Total Present:\s*([a-zA-Z0-9]+)",

        "Percentage":
            r"Your Percentages?:\s*([a-zA-Z0-9.%]+)",
    }

    row = {}

    for key, pattern in patterns.items():

        match = re.search(
            pattern,
            body_text
        )

        row[key] = (
            match.group(1).strip()
            if match
            else None
        )

    return row


def scrape_one_subject(
    driver,
    wait,
    subject_name
):
    """
    Scrape attendance for one subject.
    """

    # Apply optional filters
    set_dropdown_if_specified(
        driver,
        wait,
        "Select Course",
        COURSE
    )

    set_dropdown_if_specified(
        driver,
        wait,
        "Select Batch",
        BATCH
    )

    set_dropdown_if_specified(
        driver,
        wait,
        "Select Division",
        DIVISION
    )

    set_dropdown_if_specified(
        driver,
        wait,
        "Select Semester",
        SEMESTER
    )

    # Select subject
    set_dropdown_if_specified(
        driver,
        wait,
        "Select Subjects",
        subject_name
    )

    # View attendance
    find_button(
        driver,
        "View Attendance"
    ).click()

    # Wait until page contains subject
    wait.until(
        lambda d:
        subject_name.lower()
        in d.find_element(
            By.TAG_NAME,
            "body"
        ).text.lower()
    )

    body_text = driver.find_element(
        By.TAG_NAME,
        "body"
    ).text

    row = extract_summary(
        body_text
    )

    # Go back
    find_button(
        driver,
        "Go Back"
    ).click()

    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//*[contains("
                "text(),"
                "'Select Subject For Attendance'"
                ")]"
            )
        )
    )

    time.sleep(1)

    return row


# ============================================================
# CLEAN DATA
# ============================================================

def clean_and_validate_data(rows):
    """
    Clean scraped attendance data.
    """

    cleaned_rows = []

    for row in rows:

        # Critical fields
        if not all([
            row.get("Subject"),
            row.get("Total Attendances"),
            row.get("Total Present")
        ]):
            continue

        # Invalid values
        if any(
            value
            and isinstance(value, str)
            and value.lower()
            in [
                "undefined",
                "none",
                "nan",
                ""
            ]
            for value in row.values()
        ):
            continue

        try:

            total_attendances = int(
                row["Total Attendances"]
            )

            total_present = int(
                row["Total Present"]
            )

            # Ignore zero attendance records
            if total_attendances == 0:
                continue

            # Convert percentage
            percentage_str = row.get(
                "Percentage",
                "0%"
            )

            if (
                isinstance(
                    percentage_str,
                    str
                )
                and percentage_str.endswith("%")
            ):

                percentage = float(
                    percentage_str.rstrip("%")
                )

            else:

                percentage = float(
                    percentage_str or 0
                )

            cleaned_rows.append({

                "subject":
                    row["Subject"],

                "total_attendances":
                    total_attendances,

                "total_present":
                    total_present,

                "percentage":
                    percentage
            })

        except (
            ValueError,
            TypeError
        ) as e:

            logger.warning(
                f"Could not convert data for "
                f"subject {row.get('Subject')}: {e}"
            )

            continue

    return cleaned_rows


# ============================================================
# STATISTICS
# ============================================================

def calculate_stats(cleaned_rows):
    """
    Calculate overall attendance statistics.
    """

    if not cleaned_rows:
        return None

    total_attendances = sum(
        row["total_attendances"]
        for row in cleaned_rows
    )

    total_present = sum(
        row["total_present"]
        for row in cleaned_rows
    )

    if total_attendances == 0:

        overall_percentage = 0

    else:

        overall_percentage = round(
            (
                total_present
                / total_attendances
            ) * 100,
            2
        )

    avg_percentage = round(
        sum(
            row["percentage"]
            for row in cleaned_rows
        )
        / len(cleaned_rows),
        2
    )

    return {

        "total_attendances":
            total_attendances,

        "total_present":
            total_present,

        "overall_percentage":
            overall_percentage,

        "avg_percentage":
            avg_percentage,

        "subjects_count":
            len(cleaned_rows)
    }


# ============================================================
# SAVE ATTENDANCE
# ============================================================

def save_to_mongodb(
    db,
    username,
    email,
    track,
    cleaned_rows,
    stats
):
    """
    Insert or update attendance result.
    """

    results_collection = db[
        ATTENDANCE_RESULTS_COLLECTION
    ]

    document = {

        "username":
            username,

        "email":
            email,

        "track":
            track,

        "scraped_at":
            datetime.now(timezone.utc),

        "subjects":
            cleaned_rows,

        "statistics":
            stats,

        "total_subjects_scraped":
            len(cleaned_rows)
    }

    try:

        existing = results_collection.find_one({
            "email": email
        })

        if existing:

            result = results_collection.update_one(
                {
                    "_id":
                        existing["_id"]
                },
                {
                    "$set":
                        document
                }
            )

            logger.info(
                f"  ✓ Updated results for "
                f"{username} "
                f"({result.modified_count} document updated)"
            )

            return existing["_id"]

        else:

            result = results_collection.insert_one(
                document
            )

            logger.info(
                f"  ✓ Added new results for "
                f"{username} "
                f"(ID: {result.inserted_id})"
            )

            return result.inserted_id

    except PyMongoError as e:

        logger.error(
            f"  ✗ Failed to store results "
            f"for {username}: {e}"
        )

        raise


# ============================================================
# SCRAPE ONE USER
# ============================================================

def scrape_user(
    driver,
    db,
    user
):
    """
    Scrape attendance for one student.

    IMPORTANT FLOW:

        user.track
            ↓
        tracks collection
            ↓
        allowed subjects
            ↓
        website subjects
            ↓
        filter
            ↓
        scrape
    """

    email = user.get("email")
    password = user.get("password")
    track = user.get("track")

    username = extract_username_from_email(
        email
    )

    logger.info(
        f"\n--- Scraping user: "
        f"{username} ({email}) ---"
    )

    # --------------------------------------------------------
    # Validate user
    # --------------------------------------------------------

    if not email or not password:

        logger.error(
            f"✗ User {username} "
            f"missing email or password"
        )

        return False

    if not track:

        logger.error(
            f"✗ User {username} "
            f"has no track assigned"
        )

        return False

    logger.info(
        f"  Student track: {track}"
    )

    wait = WebDriverWait(
        driver,
        WAIT_TIMEOUT
    )

    results = []

    try:

        # ----------------------------------------------------
        # 1. Get subjects allowed for student's track
        # ----------------------------------------------------

        allowed_subjects = get_subjects_for_track(
            db,
            track
        )

        if not allowed_subjects:

            logger.error(
                f"✗ No subjects available "
                f"for track '{track}'"
            )

            return False

        logger.info(
            "  Allowed subjects:"
        )

        for subject in allowed_subjects:

            logger.info(
                f"    ✓ {subject}"
            )

        # ----------------------------------------------------
        # 2. Login
        # ----------------------------------------------------

        login(
            driver,
            wait,
            email,
            password
        )

        # ----------------------------------------------------
        # 3. Open subject selector
        # ----------------------------------------------------

        open_subject_selector(
            driver,
            wait
        )

        # ----------------------------------------------------
        # 4. Set common filters
        # ----------------------------------------------------

        set_dropdown_if_specified(
            driver,
            wait,
            "Select Course",
            COURSE
        )

        set_dropdown_if_specified(
            driver,
            wait,
            "Select Batch",
            BATCH
        )

        set_dropdown_if_specified(
            driver,
            wait,
            "Select Division",
            DIVISION
        )

        set_dropdown_if_specified(
            driver,
            wait,
            "Select Semester",
            SEMESTER
        )

        # ----------------------------------------------------
        # 5. Get subjects from website
        # ----------------------------------------------------

        website_subjects = get_subject_names(
            driver,
            wait
        )

        logger.info(
            f"  Website has "
            f"{len(website_subjects)} subject(s)"
        )

        # ----------------------------------------------------
        # 6. FILTER WEBSITE SUBJECTS BY TRACK
        # ----------------------------------------------------

        subjects = filter_subjects_by_track(
            website_subjects,
            allowed_subjects
        )

        logger.info(
            f"  After track filtering: "
            f"{len(subjects)} subject(s)"
        )

        # ----------------------------------------------------
        # 7. Show exactly what will be scraped
        # ----------------------------------------------------

        if not subjects:

            logger.warning(
                f"✗ No matching subjects found "
                f"for track '{track}'"
            )

            logger.warning(
                "  Website subjects:"
            )

            for subject in website_subjects:

                logger.warning(
                    f"    WEBSITE: {subject}"
                )

            return False

        logger.info(
            "  Subjects that WILL be scraped:"
        )

        for subject in subjects:

            logger.info(
                f"    → {subject}"
            )

        # ----------------------------------------------------
        # 8. Scrape only filtered subjects
        # ----------------------------------------------------

        for subject in subjects:

            logger.info(
                f"    Scraping: {subject}"
            )

            for attempt in range(2):

                try:

                    row = scrape_one_subject(
                        driver,
                        wait,
                        subject
                    )

                    results.append(row)

                    break

                except StaleElementReferenceException:

                    if attempt == 1:
                        raise

                    logger.warning(
                        f"    Retrying stale "
                        f"subject: {subject}"
                    )

                    continue

                except Exception as e:

                    logger.warning(
                        f"    ⚠ Skipped "
                        f"'{subject}': {e}"
                    )

                    break

        # ----------------------------------------------------
        # 9. Validate results
        # ----------------------------------------------------

        if not results:

            logger.warning(
                f"✗ No data scraped "
                f"for {username}"
            )

            return False

        # ----------------------------------------------------
        # 10. Clean data
        # ----------------------------------------------------

        cleaned_rows = clean_and_validate_data(
            results
        )

        if not cleaned_rows:

            logger.warning(
                f"✗ All subjects filtered out "
                f"for {username}"
            )

            return False

        # ----------------------------------------------------
        # 11. Calculate statistics
        # ----------------------------------------------------

        stats = calculate_stats(
            cleaned_rows
        )

        logger.info(
            f"  Statistics: "
            f"{stats['subjects_count']} subjects, "
            f"{stats['overall_percentage']}% attendance"
        )

        # ----------------------------------------------------
        # 12. Save to MongoDB
        # ----------------------------------------------------

        save_to_mongodb(
            db,
            username,
            email,
            track,
            cleaned_rows,
            stats
        )

        return True

    except Exception as e:

        logger.error(
            f"✗ Error scraping "
            f"{username}: {e}"
        )

        return False


# ============================================================
# MAIN
# ============================================================

def main():
    """
    Main function.

    Every student gets their own fresh Chrome session.
    """

    client = None

    # --------------------------------------------------------
    # CONNECT TO MONGODB
    # --------------------------------------------------------

    try:

        client = connect_mongodb()

        db = client[DB_NAME]

    except Exception as e:

        logger.error(
            f"Failed to connect to MongoDB: {e}"
        )

        sys.exit(1)

    # --------------------------------------------------------
    # GET USERS
    # --------------------------------------------------------

    try:

        users = get_users_from_mongodb(
            db
        )

        if not users:

            logger.error(
                "No users to scrape"
            )

            sys.exit(1)

    except Exception as e:

        logger.error(
            f"Failed to fetch users: {e}"
        )

        sys.exit(1)

    # --------------------------------------------------------
    # PROCESS USERS
    # --------------------------------------------------------

    successful = 0
    failed = 0

    try:

        for index, user in enumerate(
            users,
            start=1
        ):

            email = user.get(
                "email",
                "<unknown>"
            )

            driver = None
            user_success = False

            logger.info(
                f"\n{'=' * 70}"
            )

            logger.info(
                f"USER {index}/{len(users)}"
            )

            logger.info(
                f"Email: {email}"
            )

            logger.info(
                f"Track: {user.get('track', '<NO TRACK>')}"
            )

            logger.info(
                f"{'=' * 70}"
            )

            try:

                # Fresh browser for every user
                driver = build_driver()

                user_success = scrape_user(
                    driver,
                    db,
                    user
                )

            except Exception as e:

                logger.error(
                    f"Exception for user "
                    f"{email}: {e}"
                )

            finally:

                # Always close browser
                if driver is not None:

                    try:

                        driver.quit()

                    except Exception as e:

                        logger.warning(
                            f"Could not close "
                            f"browser for "
                            f"{email}: {e}"
                        )

            # ------------------------------------------------
            # Result
            # ------------------------------------------------

            if user_success:

                successful += 1

                logger.info(
                    f"✓ User completed successfully: "
                    f"{email}"
                )

            else:

                failed += 1

                logger.error(
                    f"✗ User failed: "
                    f"{email}"
                )

            # Delay between users
            if index < len(users):

                time.sleep(2)

    finally:

        if client:

            client.close()

        logger.info(
            "\n" + "=" * 70
        )

        logger.info(
            "SCRAPING COMPLETE"
        )

        logger.info(
            f"Successful: {successful}"
        )

        logger.info(
            f"Failed: {failed}"
        )

        logger.info(
            f"Total: {successful + failed}"
        )

        logger.info(
            "=" * 70
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
