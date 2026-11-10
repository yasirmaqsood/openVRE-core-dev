.PHONY: test image

test:
	python3 -m unittest discover -s tests -v

image:
	docker build -t openvre-scheduler:dev .
