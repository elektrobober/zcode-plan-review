.PHONY: check test-live
check:
	uvx --from ruff==0.14.4 ruff check .
	uvx --from ruff==0.14.4 ruff format --check .
	python3 -m unittest discover -v
test-live:
	./test-live zcode
