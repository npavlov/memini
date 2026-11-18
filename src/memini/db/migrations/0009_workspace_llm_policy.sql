-- The LLM policy of a workspace: principle 8, "a cloud provider is an explicit opt-in per
-- workspace and can be denied per source". docs/requirements.md, sections 3 and 9 (LLM providers
-- and the policy gate). The code is memini.llm.
--
-- Content goes to an LLM only as far as the strictest of two policies allows: the workspace's,
-- set here, and the llm_policy of every source the content came from (0002). The order is
-- none < local < cloud.
--
-- none: no LLM reads anything in the workspace. local: the provider the settings declare local.
-- cloud: the workspace has opted in to the provider the settings declare cloud, for content whose
-- sources all have llm_policy cloud; other content still goes to the local provider only.
--
-- Default local, as for sources: a cloud provider is always an explicit choice. Only the CLI
-- changes it (`memini workspace llm`), working on the database directly; the HTTP API does not, so
-- no token, not even an admin one, can opt a workspace in.

ALTER TABLE workspace
    ADD COLUMN llm_policy text NOT NULL DEFAULT 'local',
    ADD CONSTRAINT workspace_llm_policy_check CHECK (llm_policy IN ('none', 'local', 'cloud'));
