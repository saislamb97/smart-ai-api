# Smart AI API

## Overview
Smart AI API is a high-performance, scalable, and lightweight AI-powered REST API built using **FastAPI**. It is designed to provide AI-driven functionalities such as text analysis, image processing, and data insights.

## Features
- **Fast & Asynchronous:** Powered by FastAPI for high-speed API responses.
- **AI-Powered:** Supports AI-driven features such as NLP, image processing, and predictions.
- **Secure Authentication:** Uses JWT-based authentication.
- **Scalable:** Supports easy deployment with Docker and cloud services.
- **Detailed Documentation:** Automatically generated Swagger UI and Redoc.

## Installation

### Prerequisites
Ensure you have the following installed:
- Python 3.8+
- pip
- Virtual environment (optional but recommended)

### Setup
1. Clone the repository:
   ```bash
   git clone https://github.com/saislamb97/smart-ai-api.git
   cd smart-ai-api
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate   # On Windows use: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the application:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

## API Documentation
Once the server is running, access API documentation at:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)