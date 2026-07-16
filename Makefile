PYTHON ?= python

test:
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m py_compile mega_raid_exporter.py tests/test_exporter.py

run:
	$(PYTHON) mega_raid_exporter.py

docker-build:
	docker build -t mega-raid-exporter:dev .
