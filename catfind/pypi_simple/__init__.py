"""
PyPI Simple Repository API client library

A significantly stripped-down version of <https://pypi-simple.rtfd.io>.
"""

#: The base URL for PyPI's simple API
PYPI_SIMPLE_ENDPOINT: str = "https://pypi.org/simple/"

#: The maximum supported simple repository version (See :pep:`629`)
SUPPORTED_REPOSITORY_VERSION: str = "1.0"

from .classes import Link  # noqa: E402
from .client import PyPISimple  # noqa: E402
from .parse_stream import parse_links_stream, parse_links_stream_response  # noqa: E402
from .util import UnsupportedRepoVersionError  # noqa: E402

__all__ = [
    "Link",
    "PYPI_SIMPLE_ENDPOINT",
    "PyPISimple",
    "SUPPORTED_REPOSITORY_VERSION",
    "UnsupportedRepoVersionError",
    "parse_links_stream",
    "parse_links_stream_response",
]
