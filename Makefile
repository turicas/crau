clean:
	rm -rf .egg-info build dist

release: clean
	python setup.py sdist bdist_wheel
	twine upload dist/*

.PHONY:	clean release
