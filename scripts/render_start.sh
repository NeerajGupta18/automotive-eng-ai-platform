#!/usr/bin/env bash
# Start the whole platform in ONE container (Render free web service, or any single-port host):
#   1. FastAPI backend on 127.0.0.1:8000 (not exposed)
#   2. wait until it is healthy
#   3. load the demo data (idempotent; skipped with DEMO_SEED=0)
#   4. Streamlit UI on $PORT (the only public port)
set -euo pipefail
cd "$(dirname "$0")/.."

export DATA_DIR="${DATA_DIR:-/tmp/aeai-storage}"
export API_URL="http://127.0.0.1:8000"
UI_PORT="${PORT:-8501}"

echo "Starting API (data dir: $DATA_DIR)"
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --no-server-header &

python - <<'PY'
import sys, time, urllib.request
for _ in range(120):
    try:
        if urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2).status == 200:
            print("API is up")
            sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit("API did not become healthy within 120 s")
PY

if [ "${DEMO_SEED:-1}" = "1" ]; then
  python scripts/seed_demo.py || echo "Demo seeding failed; the app still runs without demo data"
fi

echo "Starting UI on port $UI_PORT"
exec python -m streamlit run ui/streamlit_app.py \
  --server.port "$UI_PORT" --server.address 0.0.0.0 \
  --server.headless true --browser.gatherUsageStats false
