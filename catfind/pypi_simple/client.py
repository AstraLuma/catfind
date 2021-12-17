from typing import Iterator, Tuple, Union

import httpx

from . import PYPI_SIMPLE_ENDPOINT
from .parse_stream import parse_links_stream_response

from ..http import client as catfind_client


class PyPISimple:
    """
    A client for fetching package information from a Python simple package
    repository.

    A `PyPISimple` instance can be used as a context manager that will
    automatically close its session on exit, regardless of where the session
    object came from.

    .. versionchanged:: 0.8.0
        Now usable as a context manager

    .. versionchanged:: 0.5.0
        ``session`` argument added

    .. versionchanged:: 0.4.0
        ``auth`` argument added

    :param str endpoint: The base URL of the simple API instance to query;
        defaults to the base URL for PyPI's simple API

    :param auth: Optional login/authentication details for the repository;
        either a ``(username, password)`` pair or `another authentication
        object accepted by requests
        <https://requests.readthedocs.io/en/master/user/authentication/>`_

    :param session: Optional `requests.Session` object to use instead of
        creating a fresh one
    """
    c: httpx.Client

    def __init__(
        self,
        endpoint: str = PYPI_SIMPLE_ENDPOINT,
    ) -> None:
        self.endpoint: str = endpoint.rstrip("/") + "/"
        self.cman = catfind_client()

    def __enter__(self) -> "PyPISimple":
        self.c = self.cman.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.cman.__exit__(*exc)

    def stream_project_names(
        self,
        chunk_size: int = 65535,
        timeout: Union[float, Tuple[float, float], None] = None,
    ) -> Iterator[str]:
        """
        .. versionadded:: 0.7.0

        Returns a generator of names of projects available in the repository.
        The names are not normalized.

        Unlike `get_index_page()` and `get_projects()`, this function makes a
        streaming request to the server and parses the document in chunks.  It
        is intended to be faster than the other methods, especially when the
        complete document is very large.

        .. warning::

            This function is rather experimental.  It does not have full
            support for web encodings, encoding detection, or handling invalid
            HTML.

        :param int chunk_size: how many bytes to read from the response at a
            time
        :param timeout: optional timeout to pass to the ``requests`` call
        :type timeout: Union[float, Tuple[float,float], None]
        :rtype: Iterator[str]
        :raises requests.HTTPError: if the repository responds with an HTTP
            error code
        :raises UnsupportedRepoVersionError: if the repository version has a
            greater major component than the supported repository version
        """
        with self.c.stream("GET", self.endpoint, timeout=timeout) as r:
            r.raise_for_status()
            for link in parse_links_stream_response(r, chunk_size):
                yield link.text
