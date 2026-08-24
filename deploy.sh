#!/bin/bash
# Build and start the stack. Add --migrate to also apply migrations and run the seeds.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "error: .env is missing — copy .env.example and fill it in." >&2
  exit 1
fi

docker compose up --build -d

echo "waiting for postgres…"
for _ in $(seq 1 30); do
  if docker compose exec -T postgis pg_isready -U ukraine -d ukraine >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker compose exec -T web alembic upgrade head

if [ "${1:-}" = "--migrate" ]; then
  docker compose exec -T web python scripts/seed_sources.py
  docker compose exec -T web python scripts/seed_gazetteer.py
  docker compose exec -T web python scripts/create_admin.py || true
fi

echo
echo "public site : http://localhost"
echo "admin       : http://localhost/admin"
echo "health      : http://localhost/healthz"
docker compose ps
