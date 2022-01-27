clean:
	rm -rf .egg-info build dist

fix-imports:
	autoflake --in-place --recursive --remove-unused-variables --remove-all-unused-imports .
	isort -rc .
	black .

release: clean
	python setup.py sdist bdist_wheel
	twine upload dist/*

.PHONY:	clean fix-imports release
