from mvn_upgrader.redact import redact


def test_redact_known_secret():
    assert redact("token=hunter2 here", ["hunter2"]) == "token=*** here"


def test_redact_gitlab_pat_pattern():
    out = redact("leaked glpat-ABCDEFGHIJKLMNOPQRSTUV in log")
    assert "glpat-ABCDEFGHIJKLMNOPQRSTUV" not in out
    assert "***" in out


def test_redact_openai_key_pattern():
    out = redact("key sk-ABCDEFGHIJKLMNOPQRSTUVWX done")
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in out


def test_redact_authorization_header():
    out = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "Authorization: Bearer ***" in out


def test_redact_private_token_header():
    out = redact("PRIVATE-TOKEN: glpat-xyz123")
    assert "glpat-xyz123" not in out


def test_redact_empty():
    assert redact("", ["x"]) == ""
    assert redact("nothing secret", []) == "nothing secret"
