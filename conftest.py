# Root conftest: makes pytest put the repo root on sys.path so the test
# suite's `import backend.*` works under a bare `pytest backend/tests`
# invocation (python -m pytest gets this via cwd; CI's bare pytest does not).
