import json

from ha_syncapp.config import load_config


def test_rollback_transport_is_explicit_and_defaults_to_token(tmp_path) -> None:
    default_path = tmp_path / "default.json"
    default_path.write_text("{}")
    assert load_config(default_path).repo_b_rollback_transport == "token"

    selected_path = tmp_path / "selected.json"
    selected_path.write_text(
        json.dumps(
            {
                "repo_b": "Owner/Home",
                "github_token": "test-token",
                "repo_b_rollback_transport": "deploy_key",
            }
        )
    )
    assert load_config(selected_path).repo_b_rollback_transport == "deploy_key"
