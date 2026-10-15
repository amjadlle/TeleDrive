"""Project-wide pytest bootstrap: keep bytecode caches out of the working tree.

The owner disabled .pyc generation; this conftest makes pytest runs respect
that choice without relying on PYTHONDONTWRITEBYTECODE being exported.
"""
import sys

sys.dont_write_bytecode = True
