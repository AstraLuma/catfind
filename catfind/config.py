"""
Responsible for all configuration-related things.
"""
import os
from urllib.parse import urlparse

if 'DATABASE_URL' in os.environ:
    bits = urlparse(os.environ['DATABASE_URL'])

    PONY = {
        'provider': bits.scheme,
        'user': bits.username,
        'password': bits.password,
        'host': bits.hostname,
        'database': bits.path.lstrip('/'),
    }
else:
    PONY = {
        'provider': 'sqlite',
        'filename': 'db.db3',
        'create_db': True,
    }

# Inventories to load at start
INITIAL_INVENTORIES = [
    # CPython, pretty important and undiscoverable
    'https://docs.python.org/3/objects.inv',
    # Discovery turns these up a bunch, so let's just add them now
    'https://pip.pypa.io/en/stable/objects.inv',
    'https://tox.wiki/en/latest/objects.inv',
]

# User Agent to use for making HTTP requests
USER_AGENT = "catfind <https://sphinx.rip/> (@AstraLuma)"


# Token for use with Read The Docs
RTD_TOKEN = os.environ.get("RTD_TOKEN", None)
