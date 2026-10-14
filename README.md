# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service for employee attendance tracking, manual regularization, and high-performance analytics.

## Tech Stack
- **Framework**: FastAPI (Python 3.11+)
- **Database**: MongoDB 6.0+ (Tested with 8.0)
- **Driver**: PyMongo 4.7+
- **Data Validation**: Pydantic v2

## Setup & Running Locally

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables** (Optional - defaults to `mongodb://localhost:27017` and `attendance_db`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed Sample Data**:
   ```bash
   python sample_seed.py
   ```

4. **Start the Server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. **API Documentation & Health Check**:
   - Interactive Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
   - Health check: [http://localhost:8000/health](http://localhost:8000/health)

## Deliverables & Documentation
- `app/main.py`: Complete application code with all endpoints, models, business rules (R1-R10), and automatic startup database index creation.
- `REVIEW.md`: Comprehensive audit of starter defects with symptoms, reproduction steps, and fixes.
- `DECISIONS.md`: Architectural decisions covering indexes, concurrency race condition handling, competition ranking with ties, zero-log headcount preservation, and 100x scaling.
- `requirements.txt`: Python dependencies.
