from harness.state import state_key


def test_state_key_from_remote_not_basename():
    a = state_key("https://github.com/alice/api.git", "/workspace/repo")
    b = state_key("git@github.com:bob/api.git", "/workspace/repo")
    assert a == "github.com_alice_api"
    assert a != b
    assert state_key("git@github.com:alice/api.git", "/elsewhere/api") == a


def test_state_key_falls_back_to_basename():
    assert state_key("", "/home/me/code/api") == "api"
    assert state_key("/srv/git/api.git", "/workspace/repo") == "api"
