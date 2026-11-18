"""Shared by the redaction tests: fake secrets and card numbers.

Every secret here is fake, and every token-shaped one is joined from parts at run time
(``fake("ghp_", …)``), so that no secret scanner takes the repository for a leak. The card
numbers are the public test numbers of the card networks.
"""

import base64


def fake(*parts: str) -> str:
    """A secret-shaped value, joined at run time."""
    return "".join(parts)


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


PEM_BODY = ("MIIEFAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY", "FAKEKEYFAKEKEYFAKEKEYFAKEKEY==")
PEM = fake(
    "-----BEGIN ",
    "RSA PRIVATE KEY-----\n",
    f"{PEM_BODY[0]}\n{PEM_BODY[1]}\n",
    "-----END RSA PRIVATE KEY-----",
)
OPENSSH = fake(
    "-----BEGIN ",
    "OPENSSH PRIVATE KEY-----\n",
    "b3BlbnNzaC1rZXktdjEAAAAAFAKEFAKEFAKEFAKEFAKE\n",
    "-----END OPENSSH PRIVATE KEY-----\n",
)
AWS_KEY_ID = fake("AKIA", "FAKEFAKEFAKEFAKE")
AWS_SECRET = fake("FakeSecretKey0", "FakeSecretKey0", "FakeSecret01")  # 40 characters
GITHUB = fake("ghp_", "Fake" * 9)
GITHUB_PAT = fake("github_pat_", "Fake_" * 5)
GITLAB = fake("glpat-", "FakeFakeFakeFakeFake")
SLACK = fake("xoxb-", "0000-0000-", "FakeFakeFake")
SLACK_HOOK = fake("https://hooks.slack.com/", "services/T0000/B0000/FakeFakeFake")
STRIPE = fake("sk_", "live_", "FakeFakeFakeFake")
STRIPE_TEST = fake("sk_", "test_", "FakeFakeFakeFake")
WEBHOOK_SECRET = fake("whsec_", "FakeFakeFakeFake")
GOOGLE = fake("AIza", "Fake" * 8, "Fak")
OPENAI = fake("sk-", "proj-", "Fake1Fake2Fake3Fake4Fake5")
ANTHROPIC = fake("sk-", "ant-", "api03-", "Fake1Fake2Fake3Fake4")
NPM = fake("npm_", "Fake1" * 7, "F")
JWT = ".".join(
    (b64url('{"alg":"HS256","typ":"JWT"}'), b64url('{"sub":"fake-user"}'), b64url("fake-sig"))
)
MEMINI = fake("memini_", "Fake0" * 8, "Fak")
PEM_ESCAPED = PEM.replace("\n", "\\n")  # as in code or JSON: a backslash and an n
VISA = "4111 1111 1111 1111"
MASTERCARD = "5555-5555-5555-4444"
AMEX = "3782 822463 10005"
DISCOVER = "6011111111111117"
JCB = "3530111333300000"
MASTERCARD_2 = "2223003122003222"
