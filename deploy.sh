#!/bin/bash
set -e

docker-compose up --build -d

echo "Deployment complete. Flask app running at http://localhost:5001"
