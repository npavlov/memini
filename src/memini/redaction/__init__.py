"""Redaction of secrets and card numbers in the data Memini stores (principle 11).

Deterministic detectors, no model: the same text always gives the same result, and a redacted
text redacted again is the same text, so a resent record keeps its content hash (principle 6).
A value found is replaced by a marker that names its kind and nothing of it, ``[REDACTED:card]``;
no digit of a card number is kept (not even the last four: nothing here needs them, and a marker
that names the kind is enough to tell what stood there). Counts by kind are returned, never values.

Detectors (:mod:`memini.redaction.detectors`, in this order):

=================== ===========================================================================
``private_key``     PEM private key blocks (RSA, EC, OpenSSH, PKCS#8, PGP), also with ``\\n``
                    escapes as in code and JSON, and a block cut short (no END line).
``url_credentials`` The user information of ``scheme://user:password@host``, or a token in the
                    place of the user; it becomes ``REDACTED`` (``https://REDACTED@host``), as a
                    URL cannot hold brackets there.
``slack_webhook``   ``https://hooks.slack.com/services/…`` URLs.
``memini_token``    Memini bearer tokens (``memini_`` and 43 base64url characters).
``github_token``    ``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_`` and ``github_pat_`` tokens.
``gitlab_token``    ``glpat-`` and the other GitLab token prefixes.
``slack_token``     ``xoxb-``, ``xoxp-`` and the other Slack tokens, ``xapp-`` app tokens.
``stripe_key``      ``sk_live_``, ``sk_test_``, ``rk_…`` secret keys, ``whsec_`` webhook secrets.
``aws_access_key``  AWS access key ids (``AKIA``, ``ASIA`` … and 16 characters).
``gcp_api_key``     Google API keys (``AIza`` and 35 characters). The private key of a GCP
                    service-account JSON key is a PEM block, found as ``private_key``.
``api_key``         LLM provider keys (``sk-…``, ``sk-ant-…``, ``sk-proj-…``), npm and PyPI
                    tokens.
``jwt``             JSON Web Tokens (``eyJ….eyJ….…``).
``aws_secret_key``  A 40-character AWS secret access key after its name
                    (``aws_secret_access_key = …``), or in a text that holds an access key id.
``authorization``   The credentials of an ``Authorization`` header, whatever the scheme, and a
                    ``Bearer`` value of 16 or more characters anywhere.
``secret``          The value of an assignment to a name that ends in ``password``, ``passwd``,
                    ``passphrase``, ``secret``, ``api_key``, ``secret_key``, ``private_key``,
                    ``access_key``, ``access_token``, ``refresh_token`` … with ``=``, ``:``,
                    ``:=`` or ``=>``: quoted (on one line, without spaces), or bare when it holds
                    a digit or a symbol (so ``password=password`` and ``api_key =
                    settings.api_key`` in code stay). In JSON, any string under such a key.
``card``            Payment card numbers: 13 to 19 digits in a row or in groups of 3 to 6 split
                    by spaces or dashes, an issuer's prefix, a valid Luhn check digit; not inside
                    a longer token (a URL path, a decimal, an id with letters, ``+`` and a phone
                    number). Dates, times, phone numbers, timestamps and snowflake ids (they
                    start with 1) and ISBNs are not cards.
=================== ===========================================================================

Where it runs
    - **Ingest** (:func:`memini.ingest.prepare_batch`), before anything of a batch is stored or
      derived: every string of a payload, keys included, the window's cursor and the external
      URL. The content hash is of the redacted payload; the parser of the kind reads it, and the
      normalized text it renders is redacted again. An external id or a stream key is an id:
      rewriting one could make two records one, so one that holds a secret is refused instead.
      Ids inside a payload (``id``, ``message_id``) are not looked at as card numbers.
    - **Chunks** (:mod:`memini.index.writer`), for every record kind: header, content, URL and
      anchor. A file of a repository is not copied into the database, but its chunks are, and
      they are embedded and returned by search. Chunks of pushed records come from the redacted
      payload, so here redaction is a second line. The redaction version is part of the index
      fingerprint.
    - **get_file** (:mod:`memini.query.reader`): the mirror of a repository is the raw store
      and cannot be rewritten (its blob ids are content hashes); a read redacts the text it
      returns, line by line as the chunks do, so no query tool hands out a secret.

The logs have their own, narrower redaction (:mod:`memini.logs`); this module is about data.
"""

from memini.redaction.detectors import (
    DETECTORS,
    MARKER_PREFIX,
    REDACTION_KINDS,
    URL_USERINFO_MARKER,
    Detector,
    RedactionKind,
    is_card_number,
    is_id_key,
    is_marker,
    is_placeholder,
    is_secret_key,
    luhn_valid,
    marker,
)
from memini.redaction.redact import (
    REDACTION_VERSION,
    Redacted,
    RedactionCollisionError,
    add_counts,
    redact_json,
    redact_text,
)

__all__ = [
    "DETECTORS",
    "MARKER_PREFIX",
    "REDACTION_KINDS",
    "REDACTION_VERSION",
    "URL_USERINFO_MARKER",
    "Detector",
    "Redacted",
    "RedactionCollisionError",
    "RedactionKind",
    "add_counts",
    "is_card_number",
    "is_id_key",
    "is_marker",
    "is_placeholder",
    "is_secret_key",
    "luhn_valid",
    "marker",
    "redact_json",
    "redact_text",
]
