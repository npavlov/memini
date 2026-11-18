"""The detectors: what is a secret or a card number, by pattern and check, without a model.

Each :class:`Detector` is a regular expression, the group it replaces (the whole match by
default), an optional check on the found value, and literal hints: a text that contains none of
them is not searched, so most texts cost a few substring tests. :data:`DETECTORS` runs in order;
an earlier detector's marker is never matched by a later one.

The values are found by their shape, so the rules lean two ways. Where a shape is specific (a
``ghp_`` token, a PEM block, ``AKIA`` and 16 more) every match is redacted. Where a shape is
common (a 40-character base64 run, a long number, a word after ``password=``) a check or a
context must agree: a card number passes the Luhn check and starts like a card; an AWS secret
key stands next to its name or in a text that holds an AWS access key id; the value of a
``password=`` assignment is quoted, or holds a digit or a symbol, so that ``password=password``
and ``api_key = settings.api_key`` in code stay as they are.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, get_args

__all__ = [
    "DETECTORS",
    "MARKER_PREFIX",
    "REDACTION_KINDS",
    "URL_USERINFO_MARKER",
    "Detector",
    "RedactionKind",
    "is_card_number",
    "is_id_key",
    "is_marker",
    "is_placeholder",
    "is_secret_key",
    "luhn_valid",
    "marker",
]

RedactionKind = Literal[
    "private_key",
    "url_credentials",
    "slack_webhook",
    "memini_token",
    "github_token",
    "gitlab_token",
    "slack_token",
    "stripe_key",
    "aws_access_key",
    "gcp_api_key",
    "api_key",
    "jwt",
    "aws_secret_key",
    "authorization",
    "secret",
    "card",
]
"""What a redacted value was. Its marker is ``[REDACTED:<kind>]``."""

REDACTION_KINDS: Final[tuple[RedactionKind, ...]] = get_args(RedactionKind)

MARKER_PREFIX: Final = "[REDACTED:"

URL_USERINFO_MARKER: Final = "REDACTED"
"""What the user information of a URL becomes (``https://REDACTED@host/``): a URL cannot hold
brackets there, and parsers of URLs (the payload parser's among them) would refuse it."""


def marker(kind: RedactionKind) -> str:
    """What a value of ``kind`` is replaced with: ``[REDACTED:card]``."""
    return f"{MARKER_PREFIX}{kind}]"


def is_marker(value: str) -> bool:
    """Whether ``value`` starts with a marker: it was redacted already."""
    return value.startswith(MARKER_PREFIX)


@dataclass(frozen=True, slots=True)
class Detector:
    kind: RedactionKind
    pattern: re.Pattern[str]
    group: int | str = 0
    """The group replaced by the marker; the rest of the match stays (a header name, quotes)."""
    check: Callable[[str], bool] | None = None
    """Called with the group's text; ``False`` leaves the match as it is."""
    hints: tuple[str, ...] = ()
    """The text is searched only if it contains one of these (an empty tuple: always)."""
    folded: bool = False
    """The hints are lower case and tested against the lower-cased text."""
    requires: re.Pattern[str] | None = None
    """The detector runs only if the original text (before any redaction) matches this."""
    rewrite: Callable[[str], tuple[str, int]] | None = None
    """Instead of a check and one marker: the group's text with what it holds replaced, and how
    many values were (for a run of digit groups that may hold several card numbers or none)."""


# --- Checks --------------------------------------------------------------------------------------


def luhn_valid(digits: str) -> bool:
    """The Luhn check of a string of ASCII digits: the check digit of every payment card."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# Issuer prefixes of payment cards: Visa; Mastercard (51-55, 2221-2720); Maestro (50, 56-69);
# Discover and UnionPay (6...); Amex (34, 37); Diners (300-305, 309, 36, 38, 39); JCB (3528-3589);
# Mir (2200-2204). A number that starts otherwise (0, 1, 7, 8, 9; most of 2 and 3) is not a card:
# so a timestamp in milliseconds, a snowflake id (both start with 1) or an ISBN (978, 979) is not.
_ISSUER: Final = re.compile(
    r"4|5|6|3[47]|30[0-5]|309|3[689]|35(?:2[89]|[3-8][0-9])|220[0-4]"
    r"|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720"
)


def is_card_number(text: str) -> bool:
    """Whether ``text`` (digits, possibly grouped by spaces or dashes) is a payment card number:
    13 to 19 digits, a card issuer's prefix and a valid Luhn check digit."""
    digits = text.replace(" ", "").replace("-", "")
    return (
        13 <= len(digits) <= 19
        and digits.isascii()
        and digits.isdigit()
        and _ISSUER.match(digits) is not None
        and luhn_valid(digits)
    )


def _random_looking(value: str) -> bool:
    """A token rather than a word: it has a digit, or both upper and lower case letters."""
    has_digit = any(char.isdigit() for char in value)
    return has_digit or (value.lower() != value and value.upper() != value)


def _mixed(value: str) -> bool:
    """Upper and lower case letters and a digit: a random key, not an identifier or a git hash."""
    return (
        any(char.isupper() for char in value)
        and any(char.islower() for char in value)
        and any(char.isdigit() for char in value)
    )


def _url_userinfo(value: str) -> bool:
    """User information with a password (``user:pass``), or a token in the place of the user
    (``ghp_…@``); not a plain user (``git@``) or the marker."""
    return value != URL_USERINFO_MARKER and (":" in value or _random_looking(value))


def _url_marker(value: str) -> tuple[str, int]:
    return URL_USERINFO_MARKER, 1


def _credential(value: str) -> bool:
    """The credentials of an Authorization header, not a scheme left alone (``"Bearer " + t``) or
    a name in code (``AUTH_HEADER``)."""
    return len(value) >= 8 and not is_marker(value) and _random_looking(value)


_PLACEHOLDER: Final = re.compile(r"[*x•.#-]+|\$.*|\{\{.*|%\(.*|%s|<.*>", re.IGNORECASE)


def is_placeholder(value: str) -> bool:
    """Whether ``value`` stands in for a secret rather than being one: ``****``, ``xxxx``,
    ``${VAR}``, ``$VAR``, ``{{ var }}``, ``%(name)s``, ``<password>``, or a marker."""
    return is_marker(value) or _PLACEHOLDER.fullmatch(value) is not None


_UNQUOTED_NAME: Final = re.compile(r"[A-Za-z_.-]+")


def _assigned_value(value: str) -> bool:
    """The value of a ``password = …`` assignment is a secret, not a placeholder or a marker."""
    return len(value) >= 4 and not is_placeholder(value)


def _quoted_value(value: str) -> bool:
    """A quoted value with a space is a label or a message (``"Enter your password"``)."""
    return _assigned_value(value) and not any(char.isspace() for char in value)


def _unquoted_value(value: str) -> bool:
    """An unquoted value is a secret when it holds a digit or a symbol: ``hunter2``, not
    ``password`` or ``settings.api_key`` (a name in code)."""
    return _assigned_value(value) and _UNQUOTED_NAME.fullmatch(value) is None


# --- Key names -----------------------------------------------------------------------------------

_WORDS: Final = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_SECRET_WORDS: Final = frozenset({"password", "passwd", "passphrase", "secret", "apikey"})
_KEY_QUALIFIERS: Final = frozenset({"api", "private", "secret", "access", "signing", "encryption"})
_TOKEN_QUALIFIERS: Final = frozenset(
    {"access", "refresh", "id", "auth", "bearer", "session", "api", "oauth", "bot", "csrf", "xsrf"}
)


def is_id_key(name: str) -> bool:
    """Whether a JSON key names an id by its last word: ``id``, ``message_id``, ``segmentId``,
    ``ids``. An id of digits can look like a card number by chance; it is not looked at as one."""
    words = _WORDS.findall(name)
    return bool(words) and words[-1].lower() in ("id", "ids")


def is_secret_key(name: str) -> bool:
    """Whether a JSON key names a secret by its last word: ``password``, ``db_password``,
    ``client_secret``, ``apiKey``, ``x-api-key``, ``SecretAccessKey``, ``access_token``,
    ``authorization``. ``secret_name``, ``password_hint``, ``token`` (often a page token) and
    ``private_key_id`` do not."""
    words = [word.lower() for word in _WORDS.findall(name)]
    if not words:
        return False
    last = words[-1]
    before = words[-2] if len(words) > 1 else ""
    return (
        last in _SECRET_WORDS
        or last == "authorization"
        or (last == "key" and before in _KEY_QUALIFIERS)
        or (last == "token" and before in _TOKEN_QUALIFIERS)
    )


# --- The detectors -------------------------------------------------------------------------------

# A PEM private key: from BEGIN to its END (within 16 KiB, as escaped "\n" in code too), or, with
# no END, the base64 lines and headers after the BEGIN line (a key cut short is still a secret).
_PRIVATE_KEY: Final = re.compile(
    r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----"
    r"(?:.{0,16384}?-----END \1-----"
    r"|(?:(?:\s|\\[nr])*(?:[A-Za-z0-9+/=]{16,}|Proc-Type:[^\n]*|DEK-Info:[^\n]*))*)",
    re.DOTALL,
)
# scheme://user:password@ — the whole user information goes, as a token often stands for the user;
# and scheme://token@, a user of 16 or more characters.
_URL_CREDENTIALS: Final = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]{1,30}://"
    r"([^\s:/?#@\"'<>\[\]]*:[^\s/?#\"'<>]+|[^\s:/?#@\"'<>\[\]]{16,})@"
)
_SLACK_WEBHOOK: Final = re.compile(
    r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+"
)
_MEMINI_TOKEN: Final = re.compile(r"\bmemini_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
_GITHUB_TOKEN: Final = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])"
)
_GITLAB_TOKEN: Final = re.compile(
    r"\bgl(?:pat|dt|rt|ptt|oas|cbt|soat)-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
_SLACK_TOKEN: Final = re.compile(
    r"\b(?:xox[abposre]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,})(?![A-Za-z0-9-])"
)
_STRIPE_KEY: Final = re.compile(
    r"\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9+/=]{16,})(?![A-Za-z0-9])"
)
_AWS_ACCESS_KEY: Final = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b")
_GCP_API_KEY: Final = re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")
# Keys of LLM providers (sk-…, sk-ant-…, sk-proj-…), npm and PyPI tokens.
_API_KEY: Final = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,})"
    r"(?![A-Za-z0-9_-])"
)
_JWT: Final = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*(?![A-Za-z0-9_-])"
)
# An AWS secret access key after its name, or any 40-character base64 run in a text that holds an
# access key id (the pair is how the two are written down: in a .env file, a message, a ticket).
_AWS_SECRET_NAMED: Final = re.compile(
    r"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?"
    r"(?P<value>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
)
_AWS_SECRET_PAIRED: Final = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
_AUTHORIZATION: Final = re.compile(
    r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|digest|token|negotiate)\s+)?"
    r"(?P<value>[^\s\"',;\\]+)"
)
_BEARER: Final = re.compile(r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/-]{16,}=*)")
_CONFIDENTIAL_NAMES: Final = (
    r"(?:pass(?:word|wd|phrase)|(?:client[_-]?)?secret|secret[_-]?(?:access[_-]?)?key|api[_-]?key"
    r"|access[_-]?key|private[_-]?key|(?:access|refresh|auth|api|bearer|session)[_-]?token)"
)
# name = value, name: value, name := value, name => value; the name may be quoted, or the end of a
# longer one (DB_PASSWORD, aws.secret_key). The value is quoted (on one line) or runs to a space,
# a quote, a bracket or a separator.
_ASSIGNMENT_HEAD: Final = rf"(?i)(?<![A-Za-z0-9]){_CONFIDENTIAL_NAMES}[\"']?\s*(?::=|=>|[:=])\s*"
_ASSIGNMENT_QUOTED: Final = re.compile(
    _ASSIGNMENT_HEAD + r"(?P<quote>[\"'])(?P<value>(?:(?!(?P=quote))[^\n])*)(?P=quote)"
)
_ASSIGNMENT_BARE: Final = re.compile(_ASSIGNMENT_HEAD + r"(?P<value>[^\s\"'`,;&<>(){}\[\]]+)")
# 13 to 19 digits in a row, or a run of groups of 3 to 6 digits split by one kind of separator
# (4111 1111 1111 1111, 3782-822463-10005), not inside a longer token: a URL path, a decimal, a
# version, an id with letters, a phone number (+…). Groups of fewer than 3 digits are dates and
# phone numbers. A run may hold a card and more (a CVV, another card): _rewrite_cards finds them.
_CARD: Final = re.compile(
    r"(?<![0-9A-Za-z_+./-])"
    r"(?:[0-9]{13,19}|[0-9]{3,6}(?P<sep>[ -])[0-9]{3,6}(?:(?P=sep)[0-9]{3,6})*)"
    r"(?![0-9A-Za-z_/-]|[.,][0-9])"
)


def _rewrite_cards(run: str) -> tuple[str, int]:
    """The card numbers in a run of digits: the whole of 13 to 19 digits in a row; in a run of
    groups, each longest span of whole groups that starts with a group of 4 and is a card number
    (``4111 1111 1111 1111 123``: the card, not its CVV)."""
    if run.isdigit():
        return (marker("card"), 1) if is_card_number(run) else (run, 0)
    sep = " " if " " in run else "-"
    groups = run.split(sep)
    out: list[str] = []
    found = 0
    start = 0
    while start < len(groups):
        end = next(
            (
                end
                for end in range(len(groups), start + 1, -1)
                if len(groups[start]) == 4 and is_card_number("".join(groups[start:end]))
            ),
            None,
        )
        if end is None:
            out.append(groups[start])
            start += 1
        else:
            out.append(marker("card"))
            found += 1
            start = end
    return sep.join(out), found


DETECTORS: Final[tuple[Detector, ...]] = (
    Detector("private_key", _PRIVATE_KEY, hints=("PRIVATE KEY",)),
    Detector(
        "url_credentials",
        _URL_CREDENTIALS,
        group=1,
        check=_url_userinfo,
        rewrite=_url_marker,
        hints=("://",),
    ),
    Detector("slack_webhook", _SLACK_WEBHOOK, hints=("hooks.slack.com",)),
    Detector("memini_token", _MEMINI_TOKEN, check=_random_looking, hints=("memini_",)),
    Detector("github_token", _GITHUB_TOKEN, hints=("gh", "github_pat_")),
    Detector("gitlab_token", _GITLAB_TOKEN, hints=("gl",)),
    Detector("slack_token", _SLACK_TOKEN, hints=("xox", "xapp-")),
    Detector("stripe_key", _STRIPE_KEY, hints=("_live_", "_test_", "whsec_")),
    Detector("aws_access_key", _AWS_ACCESS_KEY, hints=("AKIA", "ASIA", "ABIA", "ACCA", "A3T")),
    Detector("gcp_api_key", _GCP_API_KEY, hints=("AIza",)),
    Detector("api_key", _API_KEY, check=_mixed, hints=("sk-", "npm_", "pypi-")),
    Detector("jwt", _JWT, hints=("eyJ",)),
    Detector(
        "aws_secret_key",
        _AWS_SECRET_NAMED,
        group="value",
        hints=("aws", "secret"),
        folded=True,
    ),
    Detector("aws_secret_key", _AWS_SECRET_PAIRED, check=_mixed, requires=_AWS_ACCESS_KEY),
    Detector(
        "authorization",
        _AUTHORIZATION,
        group="value",
        check=_credential,
        hints=("authorization",),
        folded=True,
    ),
    Detector(
        "authorization",
        _BEARER,
        group="value",
        check=_random_looking,
        hints=("bearer",),
        folded=True,
    ),
    Detector(
        "secret",
        _ASSIGNMENT_QUOTED,
        group="value",
        check=_quoted_value,
        hints=("pass", "secret", "key", "token"),
        folded=True,
    ),
    Detector(
        "secret",
        _ASSIGNMENT_BARE,
        group="value",
        check=_unquoted_value,
        hints=("pass", "secret", "key", "token"),
        folded=True,
    ),
    Detector("card", _CARD, rewrite=_rewrite_cards),
)
"""Every detector, in the order they run."""
