.PHONY: install api ui test seed pdfs check verify-audit backup docker sil sil-campaign mcp gate field-data field-eval
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
