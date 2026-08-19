# Flask Redis Celery Template

> This repository provides a template for building scalable Flask web applications with background task processing using Celery and Redis. It includes Docker setup for easy local development and deployment.

## Features
- **Flask** web server
- **Celery** for background tasks
- **Redis** as broker and result backend
- **Gunicorn** for production-ready Flask serving
- **Docker Compose** for orchestration

## Project Structure
```
app/                # Flask application
  __init__.py       # App and Celery initialization
  routes.py         # Web routes
celery_worker/      # Celery worker tasks
  tasks.py          # Example Celery task
Dockerfile.web      # Flask/Gunicorn container
Dockerfile.worker   # Celery worker container
requirements.txt    # Python dependencies
docker-compose.yml  # Multi-container orchestration
deploy.sh           # Deployment script
```

## Quick Start

### Prerequisites
- Docker & Docker Compose installed

### Start All Services
```bash
./deploy.sh
```
This will build and start the Flask web server, Celery worker, and Redis. The web app will be available at [http://localhost:5001](http://localhost:5001).

### Manual Docker Compose
```bash
docker-compose up --build
```

## Usage
- Visit [http://localhost:5001](http://localhost:5001) to check the Flask app is running.
- Trigger a background task: [http://localhost:5001/run-task](http://localhost:5001/run-task)
- Wait for result: You will be redirected to `/wait/<task_id>` until the task completes.

## Customization
- Add your Flask routes in `app/routes.py`.
- Add Celery tasks in `celery_worker/tasks.py`.

## Stopping Services
```bash
docker-compose down
```

## License
MIT