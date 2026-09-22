ifeq ($(CI),true)
    DOCKER_EXEC_FLAGS = -T
else
    DOCKER_EXEC_FLAGS = -it
endif
# HOST_UID, not UID: bash (as /bin/sh on some systems) refuses to assign its read-only UID variable
COMPOSE = HOST_UID=$(shell id -u) HOST_GID=$(shell id -g) docker compose
COMPOSE_EXEC = $(COMPOSE) exec $(DOCKER_EXEC_FLAGS)

bash: 					# Run bash inside `dev` container
	$(COMPOSE_EXEC) dev bash

bash-root: 				# Run bash as root inside `dev` container
	$(COMPOSE_EXEC) -u root dev bash

build:					# Build the container (installs the three browsers with `crau browser install`)
	$(COMPOSE) build

build-ci:
	# Remove definition of user as stated in <https://docs.github.com/en/actions/reference/workflows-and-actions/dockerfile-support#user>
	sed -i '/^USER dev/d' Dockerfile
	$(COMPOSE) build

build-no-cache:			# Build the container without using cache
	$(COMPOSE) build --no-cache

clean:					# Remove build artifacts
	rm -rf .egg-info build dist

fix-imports:			# Remove unused imports, sort imports and format the code
	autoflake --in-place --recursive --remove-unused-variables --remove-all-unused-imports .
	isort -rc .
	black .

release: clean			# Build and upload the package to PyPI
	python setup.py sdist bdist_wheel
	twine upload dist/*

docker-clean: stop		# Stop and remove the container, its volumes and orphans
	$(COMPOSE) down -v --remove-orphans

doctor:					# Show the browsers found inside `dev` container
	$(COMPOSE_EXEC) dev crau browser doctor

help:					# List all make commands
	@awk -F ':.*#' '/^[a-zA-Z_-]+:.*?#/ { printf "\033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST) | sort

kill:					# Force stop (kill) and remove containers
	$(COMPOSE) kill
	$(COMPOSE) rm --force

lint:					# Run `fix-imports` inside `dev` container
	$(COMPOSE_EXEC) dev make fix-imports

logs:					# Show container logs (tail)
	$(COMPOSE) logs -tf

restart: stop start		# Stop and start the container in background

smoke:					# Run the browser smoke test (public CLI against the local test site) inside `dev` container
	$(COMPOSE_EXEC) dev python tests/browser/smoke.py $(SMOKE_ARGS)

start:					# Start the container in background
	$(COMPOSE) up -d

stop:					# Stop the container
	$(COMPOSE) down

test:					# Execute `pytest` inside `dev` container
	$(COMPOSE_EXEC) dev pytest $(TEST_ARGS)

test-ci:
	$(COMPOSE) run --rm dev pytest $(TEST_ARGS)

test-unit:				# Execute only the tests that need no browser inside `dev` container
	$(COMPOSE_EXEC) dev pytest --ignore=tests/browser/integration $(TEST_ARGS)

.PHONY: bash bash-root build build-ci build-no-cache clean docker-clean doctor fix-imports help kill lint logs release restart smoke start stop test test-ci test-unit
