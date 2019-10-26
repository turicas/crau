release:
	rm -rf build/* build/*
	python setup.py sdist bdist_wheel
	twine upload dist/*

.PHONY:	release
