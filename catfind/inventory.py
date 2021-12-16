"""
    sphinx.util.inventory
    ~~~~~~~~~~~~~~~~~~~~~

    Inventory utility functions for Sphinx.

    :copyright: Copyright 2007-2021 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""
from __future__ import annotations
from collections import namedtuple
import io
import logging
import os
import re
from typing import IO, Callable, Iterator
import zlib

import httpx

from . import http

__all__ = 'Inventory',

BUFSIZE = 16 * 1024
logger = logging.getLogger(__name__)
ENCODING = 'utf-8'


class InventoryItem(namedtuple('InventoryItemBase', [
    'name', 'type', 'prio', 'location', 'dispname',
])):
    @property
    def domain_role(self):
        domain, _, role = self.type.partition(':')
        return domain, role

    @property
    def display_name(self):
        if self.dispname == '-':
            return self.name
        else:
            return self.dispname


class InventoryFileReader:
    """A file reader for an inventory file.

    This reader supports mixture of texts and compressed texts.
    """

    def __init__(self, stream: IO) -> None:
        self.stream = stream
        self.buffer = b''
        self.eof = False

    def read_buffer(self) -> None:
        chunk = self.stream.read(BUFSIZE)
        if chunk == b'':
            self.eof = True
        self.buffer += chunk

    def readline(self) -> str:
        pos = self.buffer.find(b'\n')
        if pos != -1:
            line = self.buffer[:pos].decode(ENCODING)
            self.buffer = self.buffer[pos + 1:]
        elif self.eof:
            line = self.buffer.decode(ENCODING)
            self.buffer = b''
        else:
            self.read_buffer()
            line = self.readline()

        return line

    def readlines(self) -> Iterator[str]:
        while not self.eof:
            line = self.readline()
            if line:
                yield line

    def read_compressed_chunks(self) -> Iterator[bytes]:
        decompressor = zlib.decompressobj()
        while not self.eof:
            self.read_buffer()
            yield decompressor.decompress(self.buffer)
            self.buffer = b''
        yield decompressor.flush()

    def read_compressed_lines(self) -> Iterator[str]:
        buf = b''
        for chunk in self.read_compressed_chunks():
            buf += chunk
            pos = buf.find(b'\n')
            while pos != -1:
                yield buf[:pos].decode(ENCODING)
                buf = buf[pos + 1:]
                pos = buf.find(b'\n')


class Inventory(list):
    projname: str
    version: str

    @classmethod
    def load_uri(cls, uri: str) -> Inventory:
        """Load a URI with httpx

        Args:
            uri: URL to load from

        Returns:
            Inventory: Instance containing loaded data
        """
        # TODO: Stream directly instead of buffering the whole thing
        with http.client() as client:
            resp = client.get(uri, follow_redirects=True)
            resp.raise_for_status()
            buf = io.BytesIO(resp.content)
            self = cls.load(
                buf, uri, lambda base, tail: str(httpx.URL(base).join(tail)),
            )
        # TODO: check the history for a Permanent Redirect and record that here
        self.uri = uri
        return self

    @classmethod
    def load(cls, stream: IO, uri: str, joinfunc: Callable) -> Inventory:
        reader = InventoryFileReader(stream)
        line = reader.readline().rstrip()
        if line == '# Sphinx inventory version 1':
            return cls.load_v1(reader, uri, joinfunc)
        elif line == '# Sphinx inventory version 2':
            return cls.load_v2(reader, uri, joinfunc)
        else:
            raise ValueError('invalid inventory header: %s' % line)

    @classmethod
    def load_v1(cls, stream: InventoryFileReader, uri: str, join: Callable) -> Inventory:
        self = cls()
        self.projname = stream.readline().rstrip()[11:]
        self.version = stream.readline().rstrip()[11:]
        for line in stream.readlines():
            name, type, location = line.rstrip().split(None, 2)
            location = join(uri, location)
            # version 1 did not add anchors to the location
            if type == 'mod':
                type = 'py:module'
                location += '#module-' + name
            else:
                type = 'py:' + type
                location += '#' + name
            self.append(InventoryItem(name, type, 1, location, '-'))
        return self

    @classmethod
    def load_v2(cls, stream: InventoryFileReader, uri: str, join: Callable) -> Inventory:
        self = cls()
        self.projname = stream.readline().rstrip()[11:]
        self.version = stream.readline().rstrip()[11:]
        line = stream.readline()

        # Used to patch for a bug below
        seen_modules = set()

        if 'zlib' not in line:
            raise ValueError('invalid inventory header (not compressed): %s' % line)

        for line in stream.read_compressed_lines():
            # be careful to handle names with embedded spaces correctly
            m = re.match(r'(?x)(.+?)\s+(\S+)\s+(-?\d+)\s+?(\S*)\s+(.*)',
                         line.rstrip())
            if not m:
                continue
            name, type, prio, location, dispname = m.groups()
            if ':' not in type:
                # wrong type value. type should be in the form of "{domain}:{objtype}"
                #
                # Note: To avoid the regex DoS, this is implemented in python (refs: #8175)
                continue
            if type == 'py:module' and name in seen_modules:
                # due to a bug in 1.1 and below,
                # two inventory entries are created
                # for Python modules, and the first
                # one is correct
                continue
            else:
                seen_modules.add(name)
            if location.endswith('$'):
                location = location[:-1] + name
            location = join(uri, location)
            self.append(InventoryItem(name, type, prio, location, dispname))
        return self

    def dump(self, filename: str) -> None:
        # FIXME: accept base URL
        def escape(string: str) -> str:
            return re.sub("\\s+", " ", string)

        with open(os.path.join(filename), 'wb') as f:
            # header
            f.write(('# Sphinx inventory version 2\n'
                     '# Project: %s\n'
                     '# Version: %s\n'
                     '# The remainder of this file is compressed using zlib.\n' %
                     (escape(self.projname),
                      escape(self.version))).encode(ENCODING))

            # body
            compressor = zlib.compressobj(9)
            for name, typ, prio, location, dispname in self:
                # TODO: strip base from location
                if location.endswith(name):
                    # this can shorten the inventory by as much as 25%
                    location = location[:-len(name)] + '$'
                uri = location
                if dispname == name:
                    dispname = '-'
                entry = ('%s %s %s %s %s\n' %
                         (name, typ, prio, uri, dispname))
                f.write(compressor.compress(entry.encode()))
            f.write(compressor.flush())
