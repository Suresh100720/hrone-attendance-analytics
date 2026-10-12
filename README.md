# HROne Attendance Analytics API

A FastAPI application for employee management, attendance tracking, attendance regularization, and department-level analytics using MongoDB.

## Features

- Create and list employees.
- Record employee punch-in and punch-out.
- Retrieve attendance records and regularize attendance.
- Calculate monthly employee attendance analytics.
- Generate department summaries and late-arrival leaderboards.
- View department attendance trends with a seven-day moving average.
- Inspect MongoDB query execution plans through admin explain endpoints.

## Tech Stack

- Python 3.12+
- FastAPI
- MongoDB 7.0+
- PyMongo
- Uvicorn

## Prerequisites

- Python 3.12 or newer
- MongoDB 6.0 or newer
- Git

## Setup and Run

### 1. Clone the repository

```bash
git clone https://github.com/Suresh100720/hrone-attendance-analytics.git
cd hrone-attendance-analytics
```

### 2. Create and activate a virtual environment

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root with your local MongoDB settings:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Do not commit `.env` or other secrets to Git.

### 5. Start MongoDB

If MongoDB is installed locally, ensure the MongoDB service is running.

Alternatively, use Docker:

```powershell
docker run -d --name hrone-mongo -p 27017:27017 mongo:7
```

### 6. Load sample data

```powershell
python sample_seed.py
```

### 7. Start the API

```powershell
python -m uvicorn app.main:app --port 8000 --reload
```

## API Documentation

Once the server is running, open:

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Project Structure

```text
.
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── README.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
└── sample_seed.py
```

## Configuration and Data

MongoDB connection settings are provided through `MONGO_URI` and `MONGO_DB`. Sample employee and attendance documents are provided in `sample_data/`, and `sample_seed.py` loads the sample dataset.

## Design Notes

See `DATA_MODEL.md` for the MongoDB data model, `DECISIONS.md` for implementation decisions, and `REVIEW.md` for the code review.

## License

Prepared as part of the HROne Software Engineer Trainee assignment.
