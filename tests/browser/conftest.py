"""Setup for the browser tests: plain `async def` tests (no marker needed) and importable helpers.

The rest of the suite uses pytest-asyncio in strict mode, which leaves unmarked coroutines alone;
this hook only applies to tests under this directory.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `fakes` and `testsite`


def pytest_pyfunc_call(pyfuncitem):
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    arguments = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(asyncio.wait_for(pyfuncitem.obj(**arguments), timeout=180))
    return True
