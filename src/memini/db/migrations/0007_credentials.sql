-- Credential store: the API keys and OAuth tokens the service needs to reach a source on its own
-- (the service executor of MCP recipes, per-source git credentials). docs/requirements.md,
-- sections 4 (MCP recipes), 7 and 9. The code is memini.credentials. The conventions listed at the
-- top of 0002 hold here too.
--
-- * No secret is stored in clear text (section 7). The secret fields of a credential (an API key;
--   an access token, a refresh token and a client secret) are one JSON object, encrypted with
--   AES-256-GCM under the key of the setting credential_key, which never enters the database.
--   Everything else is clear: what kind of credential it is, when its access token expires, the
--   token endpoint and the client id, and which key encrypted it.
-- * A ciphertext is bound to its row: the associated data of the encryption is the credential id,
--   the workspace, the source and the kind. A ciphertext copied into another row, or a row whose
--   kind was changed, fails to decrypt.
-- * key_id names the key a row is encrypted with: the first 16 hex digits of
--   HMAC-SHA256(key, 'memini.credential.key-id.v1'), which identifies a key without revealing it.
--   A rotation (memini credentials rotate-key) re-encrypts every row whose key_id is not the
--   current key's.
-- * generation counts the versions of the secret: setting a credential again or refreshing its
--   OAuth tokens adds one; re-encrypting under a new key does not. A refresh locks the row, calls
--   the provider and writes the new tokens with the next generation in one transaction; a second
--   refresh that waited for the lock sees the new generation and uses the new tokens instead of
--   spending the old refresh token again.
-- * One credential per source. Deleting the source deletes it.

CREATE TABLE credential (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    -- api_key: one secret value, sent as the source requires. oauth: an access token, usually a
    -- refresh token and the token endpoint to refresh it at.
    kind         text        NOT NULL CONSTRAINT credential_kind_check
                             CHECK (kind IN ('api_key', 'oauth')),
    -- What is not secret: for oauth the token endpoint, the client id and the scope. Never a
    -- token or a client secret; those are in ciphertext.
    metadata     jsonb       NOT NULL DEFAULT '{}' CONSTRAINT credential_metadata_check
                             CHECK (jsonb_typeof(metadata) = 'object'),
    -- oauth: when the access token expires, as the provider said. NULL: not said, or api_key.
    expires_at   timestamptz,
    -- The key the ciphertext is encrypted with (see above).
    key_id       text        NOT NULL CONSTRAINT credential_key_id_check
                             CHECK (key_id ~ '^[0-9a-f]{16}$'),
    -- The 96-bit nonce of AES-GCM, random per encryption.
    nonce        bytea       NOT NULL CONSTRAINT credential_nonce_check
                             CHECK (octet_length(nonce) = 12),
    -- The encrypted secret fields followed by the 16-byte authentication tag.
    ciphertext   bytea       NOT NULL CONSTRAINT credential_ciphertext_check
                             CHECK (octet_length(ciphertext) > 16),
    generation   bigint      NOT NULL DEFAULT 1 CONSTRAINT credential_generation_check
                             CHECK (generation > 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- The secret last changed: set again or refreshed. A rotation leaves it.
    updated_at   timestamptz NOT NULL DEFAULT now(),
    -- oauth: the last refresh that was stored.
    refreshed_at timestamptz,
    CONSTRAINT credential_source_fkey FOREIGN KEY (workspace_id, source_id)
        REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT credential_source_id_key UNIQUE (source_id)
);

-- Rotation: the rows not yet under the current key.
CREATE INDEX credential_key_id_idx ON credential (key_id);
-- Credentials of a workspace: listings, and deleting a workspace.
CREATE INDEX credential_workspace_idx ON credential (workspace_id);
