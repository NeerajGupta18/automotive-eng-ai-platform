.PHONY: install api ui test seed pdfs check verify-audit backup docker sil sil-campaign mcp gate field-data field-eval owner-api owner-web owner-build owner-help-eval owner-bridge owner-notify owner-vapid
install:
	pip install -r requirements-dev.txt
api:
	uvicorn app.api.main:app --reload --port 8000
ui:
	streamlit run ui/streamlit_app.py
test:
	python -m pytest -q
seed:
	python scripts/seed_demo.py
pdfs:
	python scripts/make_sample_pdfs.py
check:                                   # validate configuration before starting
	python scripts/manage.py check-config
verify-audit:
	python scripts/manage.py verify-audit
backup:
	python scripts/manage.py backup backups/platform-$$(date +%F).tar.gz
docker:
	docker compose up --build
sil:                                     # compile the AEB C code and its test mutants for software-in-the-loop runs
	python scripts/sil_run.py build --mutants
sil-campaign:                            # Phase 4 CI gate locally: requirements, back-to-back, mutation testing
	python scripts/sil_run.py campaign --mutation --min-mutation-score 0.6 --out sil-report
mcp:                                     # MCP server for AI assistants (stdio); set AEAI_PROJECT and, with auth, AEAI_API_TOKEN
	python scripts/mcp_server.py
gate:                                    # release gate against the running API (exit 1 on NO-GO)
	python scripts/release_gate.py --project $${PROJECT:-bcm-demo}
field-data:                              # regenerate the synthetic field recordings and labelled cases (Phase 6)
	python scripts/make_field_data.py
field-eval:                              # measure the field-issue agent on the labelled cases (CI gate thresholds)
	python scripts/eval_field_agent.py --check
owner-api:                               # CarSaathi owner app API on :8100 (Phase A; sign-in codes appear in this log)
	uvicorn --factory owner.api:create_app --reload --port 8100
owner-web:                               # the owner app with live reload on :5173 (calls the API on :8100)
	cd owner-app && npm install && npm run dev
owner-build:                             # build the app; then `make owner-api` serves it on http://localhost:8100
	cd owner-app && npm ci && npm run build
owner-help-eval:                         # Phase B: measure the help search on the labelled descriptions (CI gate thresholds)
	python scripts/eval_owner_help.py --check
owner-notify:                            # Phase D: the daily reminder run by hand (emails, push, inbox; each notification once)
	python -m owner.notify run
owner-vapid:                             # Phase D: print a Web Push key pair for OWNER_VAPID_PUBLIC_KEY / OWNER_VAPID_PRIVATE_KEY
	python -m owner.notify vapid
owner-bridge:                            # Phase B: owner API that sends ADAS reports to the platform API on :8000 (run `make api` first)
	OWNER_FIELD_API_URL=http://127.0.0.1:8000 OWNER_FIELD_PROJECT=$${PROJECT:-bcm-demo} OWNER_FIELD_LIVE=1 uvicorn --factory owner.api:create_app --port 8100
