from typing import Dict, List, NamedTuple, Union


class Link(NamedTuple):
    """
    .. versionadded:: 0.7.0

    A hyperlink extracted from an HTML page
    """

    #: The text inside the link tag, with leading & trailing whitespace removed
    #: and with any tags nested inside the link tags ignored
    text: str

    #: The URL that the link points to, resolved relative to the URL of the
    #: source HTML page and relative to the page's ``<base>`` href value, if
    #: any
    url: str

    #: A dictionary of attributes set on the link tag (including the unmodified
    #: ``href`` attribute).  Keys are converted to lowercase.  Most attributes
    #: have `str` values, but some (referred to as "CDATA list attributes" by
    #: the HTML spec; e.g., ``"class"``) have values of type ``List[str]``
    #: instead.
    attrs: Dict[str, Union[str, List[str]]]
