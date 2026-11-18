"""The ``llm_*`` settings of memini.config. Every value here is synthetic."""

from pathlib import Path

import pytest

from memini.config import ConfigError, environment_variables, load_settings

FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"
LOCAL_KEY = "sk-fake-local-llm-key-canary-0101"
CLOUD_KEY = "sk-fake-cloud-llm-key-canary-0202"
BOTH = {
    "MEMINI_DATABASE_URL": FAKE_URL,
    "MEMINI_LLM_LOCAL_URL": "http://127.0.0.1:11434/v1",
    "MEMINI_LLM_LOCAL_MODEL": "synthetic-chat:7b",
    "MEMINI_LLM_CLOUD_URL": "https://llm.example.com/v1",
    "MEMINI_LLM_CLOUD_MODEL": "synthetic-cloud-chat",
}


def problems(env: dict[str, str]) -> tuple[str, ...]:
    with pytest.raises(ConfigError) as info:
        load_settings(env)
    return info.value.problems


def test_no_provider_is_configured_by_default() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL})

    assert settings.llm_local_url is None
    assert settings.llm_cloud_url is None
    assert settings.llm_local_structured_output == "json_schema"
    assert settings.llm_timeout == 300.0


def test_reads_both_providers_with_keys_from_the_environment_and_files(tmp_path: Path) -> None:
    key_file = tmp_path / "cloud_key"
    key_file.write_text(CLOUD_KEY + "\n", encoding="utf-8")

    settings = load_settings(
        BOTH
        | {
            "MEMINI_LLM_LOCAL_API_KEY": LOCAL_KEY,
            "MEMINI_LLM_CLOUD_API_KEY_FILE": str(key_file),
            "MEMINI_LLM_CLOUD_STRUCTURED_OUTPUT": "json_object",
            "MEMINI_LLM_TIMEOUT": "90",
        }
    )

    assert settings.llm_local_url == "http://127.0.0.1:11434/v1"
    assert settings.llm_local_model == "synthetic-chat:7b"
    assert settings.llm_local_api_key is not None
    assert settings.llm_local_api_key.get_secret_value() == LOCAL_KEY
    assert settings.llm_cloud_api_key is not None
    assert settings.llm_cloud_api_key.get_secret_value() == CLOUD_KEY
    assert settings.llm_cloud_structured_output == "json_object"
    assert settings.llm_timeout == 90.0
    for key in (LOCAL_KEY, CLOUD_KEY):
        assert key not in repr(settings)
        assert key not in settings.model_dump_json()


def test_the_keys_are_secrets_with_file_variants() -> None:
    names = environment_variables()

    for slot in ("LOCAL", "CLOUD"):
        assert f"MEMINI_LLM_{slot}_API_KEY" in names
        assert f"MEMINI_LLM_{slot}_API_KEY_FILE" in names
        assert f"MEMINI_LLM_{slot}_URL_FILE" not in names


def test_a_key_in_the_config_file_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "memini.toml"
    config.write_text(f'llm_cloud_api_key = "{CLOUD_KEY}"\n', encoding="utf-8")

    found = problems(BOTH | {"MEMINI_CONFIG_FILE": str(config)})

    assert found == (
        f"llm_cloud_api_key (config file {config}): a secret is not read from the config file;"
        " set MEMINI_LLM_CLOUD_API_KEY or MEMINI_LLM_CLOUD_API_KEY_FILE",
    )
    assert CLOUD_KEY not in "".join(found)


def test_a_key_with_whitespace_is_refused_without_showing_it() -> None:
    found = problems(BOTH | {"MEMINI_LLM_LOCAL_API_KEY": f"{LOCAL_KEY} extra"})

    assert [problem.split(":")[0] for problem in found] == ["MEMINI_LLM_LOCAL_API_KEY"]
    assert LOCAL_KEY not in "".join(found)


def test_a_cloud_url_must_use_https() -> None:
    found = problems(BOTH | {"MEMINI_LLM_CLOUD_URL": "http://llm.example.com/v1"})

    assert len(found) == 1
    assert found[0].startswith("MEMINI_LLM_CLOUD_URL: invalid value (string_pattern_mismatch);")
    assert "https://" in found[0]


def test_settings_of_a_slot_need_its_url() -> None:
    found = problems(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_LLM_CLOUD_MODEL": "synthetic-cloud-chat",
            "MEMINI_LLM_CLOUD_API_KEY": CLOUD_KEY,
            "MEMINI_LLM_LOCAL_STRUCTURED_OUTPUT": "none",
            "MEMINI_LLM_TIMEOUT": "60",
        }
    )

    assert found == (
        "MEMINI_LLM_LOCAL_STRUCTURED_OUTPUT: has no effect without llm_local_url;"
        " set MEMINI_LLM_LOCAL_URL, or remove it",
        "MEMINI_LLM_CLOUD_MODEL: has no effect without llm_cloud_url;"
        " set MEMINI_LLM_CLOUD_URL, or remove it",
        "MEMINI_LLM_CLOUD_API_KEY: has no effect without llm_cloud_url;"
        " set MEMINI_LLM_CLOUD_URL, or remove it",
        "MEMINI_LLM_TIMEOUT: has no effect without llm_local_url or llm_cloud_url;"
        " set one, or remove it",
    )


def test_a_url_needs_its_model() -> None:
    env = dict(BOTH)
    del env["MEMINI_LLM_LOCAL_MODEL"]

    assert problems(env) == (
        "MEMINI_LLM_LOCAL_MODEL: required when llm_local_url is set;"
        " set MEMINI_LLM_LOCAL_MODEL or llm_local_model in the config file",
    )


def test_credentials_in_a_url_are_refused() -> None:
    found = problems(BOTH | {"MEMINI_LLM_LOCAL_URL": "http://user:not-a-secret@127.0.0.1/v1"})

    assert found == (
        "MEMINI_LLM_LOCAL_URL: holds credentials, but the URL is not a secret;"
        " set MEMINI_LLM_LOCAL_API_KEY or MEMINI_LLM_LOCAL_API_KEY_FILE instead",
    )


def test_one_endpoint_and_model_cannot_be_both_local_and_cloud() -> None:
    same = BOTH | {
        "MEMINI_LLM_LOCAL_URL": "https://gateway.example.com/v1",
        "MEMINI_LLM_CLOUD_URL": "https://gateway.example.com/v1/",
        "MEMINI_LLM_CLOUD_MODEL": "synthetic-chat:7b",
    }

    assert problems(same) == (
        "MEMINI_LLM_CLOUD_URL: names the same endpoint and model as llm_local_url; an endpoint"
        " is declared either local or cloud",
    )
    # One gateway serving a local and a cloud model under different names is fine.
    other_model = same | {"MEMINI_LLM_CLOUD_MODEL": "synthetic-cloud-chat"}
    assert load_settings(other_model).llm_cloud_model == "synthetic-cloud-chat"


def test_an_unknown_structured_output_mode_is_refused() -> None:
    found = problems(BOTH | {"MEMINI_LLM_LOCAL_STRUCTURED_OUTPUT": "grammar"})

    assert len(found) == 1
    assert found[0].startswith("MEMINI_LLM_LOCAL_STRUCTURED_OUTPUT: invalid value")
    assert "json_schema, json_object or none" in found[0]
