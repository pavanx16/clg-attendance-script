# Attendance Scraper

A Python scraper that logs into the attendance system, reads attendance data for active users from MongoDB, and stores the results back in MongoDB.

## Requirements

- Python 3.14 or newer
- Google Chrome
- Access to MongoDB Atlas

## Setup

1. Install the dependencies:

```bash
pip install -r requirements.txt
```

2. Create a `.env` file in the project root:

```env
MONGODB_URI=your_mongodb_connection_string
DB_NAME=attendance_db
```

`DB_NAME` is optional. If you do not set it, the app uses `attendance_db`.

## How it works

- Reads active users from the `users` collection in MongoDB
- Logs into the attendance website for each user
- Scrapes attendance data for each subject
- Saves the results in the `attendance_results` collection

## Run the scraper

```bash
python main.py
```

If you use `uv`, you can also run:

```bash
uv run python main.py
```

## Notes

- The scraper expects each user document to have `email`, `password`, and `status: "active"`
- Chrome must be installed because Selenium uses it to open the website
- The script uses headless mode by default

## Project files

- `main.py` - main scraper logic
- `test.py` - MongoDB connection test
- `src/attendance_scraper/__init__.py` - package entry point placeholder
