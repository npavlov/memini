# SYNTHETIC DATA

Everything in this directory is invented and written by hand for the prompt-injection tests and
the injection runs of the decision eval of Memini.

- The injection attempts (instructions to a model, fake system and assistant turns, fake data
  markers, role-play, encoded and invisible text, requests to call tools or to send data away)
  are invented text. None is copied from a real attack, message or document.
- It continues the invented story of `eval/comms` (see its `SYNTHETIC.md`): Saltreed
  Harbourworks, the Port of Hollin Mere, Tidewater, Beacon and every person of
  `eval/comms/people.json` are fictional, and so are the guest account `U0GUEST77`, "Accounts
  Desk" and "Гость".
- Addresses and links use reserved domains only: `*.example.test` and `*.example.org` (RFC 2606),
  the addresses the attempts want data sent to included. Ids are made up.
- No credentials, tokens or keys.

Keep it that way: a change to this directory adds invented content only (CLAUDE.md, "Never commit").
