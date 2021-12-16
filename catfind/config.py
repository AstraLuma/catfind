"""
Responsible for all configuration-related things.
"""
import os
from urllib.parse import urlparse

if 'DATABASE_URL' in os.environ:
    bits = urlparse(os.environ['DATABASE_URL'])
    PONY = {
        'provider': bits.scheme,
        'user': bits.user,
        'password': bits.password,
        'host': bits.host,
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
    'https://docs.python.org/3/objects.inv',
]

# User Agent to use for making HTTP requests
USER_AGENT = "catfind <https://sphinx.rip/>"