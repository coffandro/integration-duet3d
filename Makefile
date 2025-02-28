.PHONY: all

all: test flake8 yapf

test:
	uv run pytest --cov-config .coveragerc --cov simplyprint_duet3d tests/ -vv

flake8:
	uv run flake8 --statistics simplyprint_duet3d

yapf:
	uv run yapf --style .style.yapf -r --diff simplyprint_duet3d