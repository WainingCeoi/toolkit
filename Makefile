.PHONY: run lint fmt test

run:  ## Launch the Streamlit app
	uv run streamlit run src/app.py

lint:  ## Lint with ruff
	uv run ruff check .

fmt:  ## Auto-format with ruff
	uv run ruff format .

test:  ## Run the unit tests
	uv run pytest
