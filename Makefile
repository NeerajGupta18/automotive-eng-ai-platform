.PHONY: install api ui test seed pdfs check verify-audit backup docker
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
