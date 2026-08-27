"""
Attendance System Scraper with MongoDB Atlas Integration

Scrapes the Attendance System for multiple users stored in MongoDB Atlas,
handles React dropdowns, filters out zero-attendance/undefined rows,
and stores results directly in MongoDB with username extracted from email.
"""
from dotenv import load_dotenv

import os
import re
import sys
import time
import logging
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, ElementClickInterceptedException
from webdriver_manager.chrome import ChromeDriverManager
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
BASE_URL = "https://attendence-system-1910.vercel.app"
LOGIN_URL = f"{BASE_URL}/users/login"
HEADLESS = True  # Set to True for GitHub Actions
WAIT_TIMEOUT = 20  # seconds to wait for each page/element to appear

# MongoDB Atlas configuration from environment variables
MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "attendance_db")
USERS_COLLECTION = "users"
ATTENDANCE_RESULTS_COLLECTION = "attendance_results"

# Optional: Filter by specific filters (set to None to skip)
COURSE = None
BATCH = None
DIVISION = None
SEMESTER = None

# ==================== MONGODB SETUP ====================
def connect_mongodb():
    """Connect to MongoDB Atlas."""
    if not MONGODB_URI:
        raise RuntimeError(
            "Missing MONGODB_URI. Set it as an environment variable."
        )
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        # Verify connection
        client.admin.command('ping')
        logger.info("✓ Connected to MongoDB Atlas")
        return client
    except PyMongoError as e:
        logger.error(f"✗ MongoDB connection failed: {e}")
        raise

def get_users_from_mongodb(db):
    """Fetch all active users from MongoDB."""
    users_collection = db[USERS_COLLECTION]
    users = list(users_collection.find({"status": "active"}))

    if not users:
        logger.warning("No active users found in MongoDB")
        return []

    logger.info(f"Found {len(users)} active user(s)")
    return users

def extract_username_from_email(email):
    """Extract username from email (part before @)."""
    return email.split('@')[0]

# ==================== SELENIUM SETUP ====================
def build_driver():
    """Build and return a Selenium WebDriver."""
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--incognito")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--log-level=3")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

def xpath_quote(s):
    """XPath string quoting helper."""
    if "'" not in s:
        return f"'{s}'"
    parts = s.split("'")
    return "concat('" + "', \"'\"" + ", '".join(parts) + "')"

def find_button(driver, text):
    """Find button by text."""
    return driver.find_element(By.XPATH, f"//button[normalize-space()={xpath_quote(text)}]")

def safe_click(driver, element):
    """Fallback click if normal click is intercepted."""
    try:
        driver.execute_script("arguments[0].scrollIntoView(true);", element)
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)

def set_dropdown_if_specified(driver, wait, label_text, value):
    """React dropdown handler for setting a specific value."""
    if value is None:
        return

    xpath_label = f"//*[contains(text(), {xpath_quote(label_text)})]"
    container = wait.until(EC.presence_of_element_located(
        (By.XPATH, f"{xpath_label}/following-sibling::div | {xpath_label}/parent::*/following-sibling::div")
    ))
    safe_click(driver, container)
    time.sleep(0.5)

    option = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                f"//*[normalize-space()={xpath_quote(value)} "
                f"and not(ancestor-or-self::*[@aria-hidden='true'])]"
            )
        )
    )
    safe_click(driver, option)
    time.sleep(0.6)

def get_subject_names(driver, wait):
    """React dropdown handler to scrape all available subjects."""
    xpath_label = "//*[contains(text(), 'Select Subjects')]"
    container = wait.until(EC.presence_of_element_located(
        (By.XPATH, f"{xpath_label}/following-sibling::div | {xpath_label}/parent::*/following-sibling::div")
    ))
    safe_click(driver, container)
    time.sleep(1)

    options = driver.find_elements(By.XPATH, "//li | //*[@role='option']")

    subjects = []
    for opt in options:
        text = opt.text.strip()
        if text and text.lower() != "none" and text not in subjects:
            subjects.append(text)

    # Close the dropdown
    safe_click(driver, container)
    time.sleep(0.5)

    return subjects

# ==================== LOGIN & SCRAPING ====================
def login(driver, wait, email, password):
    """Login to the attendance system."""
    driver.get(LOGIN_URL)
    email_input = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder='john@gmail.com'], input[type='email']"))
    )
    password_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")

    email_input.clear()
    email_input.send_keys(email)
    password_input.clear()
    password_input.send_keys(password)

    find_button(driver, "Log In").click()

    try:
        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//button[normalize-space()='Your Attendances']")
        ))
        logger.info(f"  ✓ Logged in as {email}")
    except TimeoutException:
        raise RuntimeError(f"Login failed for {email}. Check credentials or site status.")

def open_subject_selector(driver, wait):
    """Open the subject selector page."""
    find_button(driver, "Your Attendances").click()
    wait.until(EC.presence_of_element_located(
        (By.XPATH, "//*[contains(text(),'Select Subject For Attendance')]")
    ))
    time.sleep(3)

def extract_summary(body_text):
    """Extract attendance summary from page text."""
    patterns = {
        "Subject": r"Subject:\s*(.+)",
        "Total Attendances": r"Total Attendances:\s*([a-zA-Z0-9]+)",
        "Total Present": r"Total Present:\s*([a-zA-Z0-9]+)",
        "Percentage": r"Your Percentages?:\s*([a-zA-Z0-9.%]+)",
    }
    row = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, body_text)
        row[key] = m.group(1).strip() if m else None
    return row

def scrape_one_subject(driver, wait, subject_name):
    """Scrape attendance for a single subject."""
    set_dropdown_if_specified(driver, wait, "Select Course", COURSE)
    set_dropdown_if_specified(driver, wait, "Select Batch", BATCH)
    set_dropdown_if_specified(driver, wait, "Select Division", DIVISION)
    set_dropdown_if_specified(driver, wait, "Select Semester", SEMESTER)
    set_dropdown_if_specified(driver, wait, "Select Subjects", subject_name)

    find_button(driver, "View Attendance").click()

    # Wait until the page loads
    wait.until(lambda d: subject_name.lower() in d.find_element(By.TAG_NAME, "body").text.lower())

    body_text = driver.find_element(By.TAG_NAME, "body").text
    row = extract_summary(body_text)

    find_button(driver, "Go Back").click()
    wait.until(EC.presence_of_element_located(
        (By.XPATH, "//*[contains(text(),'Select Subject For Attendance')]")
    ))
    time.sleep(1)

    return row

# ==================== DATA PROCESSING & MONGODB STORAGE ====================
def clean_and_validate_data(rows):
    """Clean scraped data and remove invalid entries."""
    cleaned_rows = []

    for row in rows:
        # Skip if any critical field is missing or undefined
        if not all([row.get("Subject"), row.get("Total Attendances"), row.get("Total Present")]):
            continue

        if any(val and isinstance(val, str) and val.lower() in ["undefined", "none", "nan", ""] for val in row.values()):
            continue

        # Convert to numeric for validation
        try:
            total_attendances = int(row["Total Attendances"])
            total_present = int(row["Total Present"])

            # Skip if either is 0
            if total_attendances == 0 or total_present == 0:
                continue

            # Convert percentage string to float
            percentage_str = row.get("Percentage", "0%")
            if isinstance(percentage_str, str) and percentage_str.endswith("%"):
                percentage = float(percentage_str.rstrip("%"))
            else:
                percentage = float(percentage_str or 0)

            cleaned_rows.append({
                "subject": row["Subject"],
                "total_attendances": total_attendances,
                "total_present": total_present,
                "percentage": percentage
            })
        except (ValueError, TypeError) as e:
            logger.warning(f"Could not convert data for subject {row.get('Subject')}: {e}")
            continue

    return cleaned_rows

def calculate_stats(cleaned_rows):
    """Calculate overall statistics."""
    if not cleaned_rows:
        return None

    total_attendances = sum(row["total_attendances"] for row in cleaned_rows)
    total_present = sum(row["total_present"] for row in cleaned_rows)

    if total_attendances == 0:
        overall_percentage = 0
    else:
        overall_percentage = round((total_present / total_attendances) * 100, 2)

    # Calculate average percentage across all subjects
    avg_percentage = round(sum(row["percentage"] for row in cleaned_rows) / len(cleaned_rows), 2)

    return {
        "total_attendances": total_attendances,
        "total_present": total_present,
        "overall_percentage": overall_percentage,
        "avg_percentage": avg_percentage,
        "subjects_count": len(cleaned_rows)
    }

def save_to_mongodb(db, username, email, cleaned_rows, stats):
    """Save attendance results to MongoDB."""
    results_collection = db[ATTENDANCE_RESULTS_COLLECTION]

    document = {
        "username": username,
        "email": email,
        "scraped_at": datetime.now(timezone.utc),
        "subjects": cleaned_rows,
        "statistics": stats,
        "total_subjects_scraped": len(cleaned_rows)
    }

    try:
        result = results_collection.insert_one(document)
        logger.info(f"  ✓ Stored results for {username} (ID: {result.inserted_id})")
        return result.inserted_id
    except PyMongoError as e:
        logger.error(f"  ✗ Failed to store results for {username}: {e}")
        raise

# ==================== MAIN SCRAPING WORKFLOW ====================
def scrape_user(driver, db, user):
    """
    Scrape attendance for a single user.

    Stored document structure:
    {
        "username": "student1",
        "email": "student1@gmail.com",
        "scraped_at": "2024-01-20T...",
        "subjects": [
            {
                "subject": "Mathematics",
                "total_attendances": 30,
                "total_present": 28,
                "percentage": 93.33
            }
        ],
        "statistics": {
            "total_attendances": 100,
            "total_present": 92,
            "overall_percentage": 92.0,
            "avg_percentage": 90.5,
            "subjects_count": 3
        }
    }
    """
    email = user.get("email")
    password = user.get("password")
    username = extract_username_from_email(email)

    logger.info(f"\n--- Scraping user: {username} ({email}) ---")

    if not email or not password:
        logger.error(f"✗ User {username} missing email or password")
        return False

    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    results = []

    try:
        # Login
        login(driver, wait, email, password)

        # Open subject selector
        open_subject_selector(driver, wait)

        # Set filters if specified
        set_dropdown_if_specified(driver, wait, "Select Course", COURSE)
        set_dropdown_if_specified(driver, wait, "Select Batch", BATCH)
        set_dropdown_if_specified(driver, wait, "Select Division", DIVISION)
        set_dropdown_if_specified(driver, wait, "Select Semester", SEMESTER)

        # Get all subjects
        subjects = get_subject_names(driver, wait)
        logger.info(f"  Found {len(subjects)} subject(s)")

        # Scrape each subject
        for subject in subjects:
            logger.info(f"    Scraping: {subject}")
            for attempt in range(2):
                try:
                    row = scrape_one_subject(driver, wait, subject)
                    results.append(row)
                    break
                except StaleElementReferenceException:
                    if attempt == 1:
                        raise
                    continue
                except Exception as e:
                    logger.warning(f"    ⚠ Skipped '{subject}': {e}")
                    break

        if not results:
            logger.warning(f"✗ No data scraped for {username}")
            return False

        # Clean data
        cleaned_rows = clean_and_validate_data(results)
        if not cleaned_rows:
            logger.warning(f"✗ All subjects filtered out for {username}")
            return False

        # Calculate stats
        stats = calculate_stats(cleaned_rows)
        logger.info(f"  Statistics: {stats['subjects_count']} subjects, "
                   f"{stats['overall_percentage']}% attendance")

        # Save to MongoDB
        save_to_mongodb(db, username, email, cleaned_rows, stats)

        return True

    except Exception as e:
        logger.error(f"✗ Error scraping {username}: {e}")
        return False

def main():
    """Main function to orchestrate the scraping of all users."""
    # Connect to MongoDB
    try:
        client = connect_mongodb()
        db = client[DB_NAME]
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        sys.exit(1)

    # Get users from MongoDB
    try:
        users = get_users_from_mongodb(db)
        if not users:
            logger.error("No users to scrape")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to fetch users: {e}")
        sys.exit(1)

    # IMPORTANT:
    # Create a completely fresh Chrome session for EVERY user.
    #
    # Reusing one Selenium driver between users can leak browser state
    # (cookies, localStorage, React state, navigation state, etc.) from the
    # previous account. That is the main reason the second user can fail
    # even though login succeeds.
    successful = 0
    failed = 0

    try:
        for index, user in enumerate(users, start=1):
            email = user.get("email", "<unknown>")
            driver = None
            user_success = False

            logger.info(f"\n========== USER {index}/{len(users)} ==========")

            try:
                # Fresh browser/session for this user.
                driver = build_driver()
                user_success = scrape_user(driver, db, user)

            except Exception as e:
                logger.error(f"Exception for user {email}: {e}")

            finally:
                # Always destroy the browser before moving to the next user.
                # This guarantees that the next account starts clean.
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception as e:
                        logger.warning(f"Could not close browser for {email}: {e}")

            if user_success:
                successful += 1
                logger.info(f"✓ User completed successfully: {email}")
            else:
                failed += 1
                logger.error(f"✗ User failed: {email}")

            # Small delay between completely independent browser sessions.
            if index < len(users):
                time.sleep(2)

    finally:
        client.close()
        logger.info(f"\n=== COMPLETE ===")
        logger.info(f"Successful: {successful}")
        logger.info(f"Failed: {failed}")
        logger.info(f"Total: {successful + failed}")

if __name__ == "__main__":
    main()
