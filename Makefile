.PHONY: install api ui test seed pdfs
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
