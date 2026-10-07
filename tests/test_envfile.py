"""The .env loader: what it parses, and that a real environment variable always wins."""
from app import envfile


def load(tmp_path, text, env=None):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    env = {} if env is None else env
    envfile.load(p, env)
    return env


def test_plain_pairs(tmp_path):
    assert load(tmp_path, "PRINTAPI_TOKEN=abc\nPRINT_PORT=3460\n") == {
        "PRINTAPI_TOKEN": "abc", "PRINT_PORT": "3460"}


def test_skips_blanks_and_comments(tmp_path):
    assert load(tmp_path, "\n# a comment\n   \nA=1\n  # indented comment\n") == {"A": "1"}


def test_export_prefix_and_whitespace(tmp_path):
    assert load(tmp_path, "export A = 1 \n") == {"A": "1"}


def test_quotes_are_stripped_and_keep_hashes(tmp_path):
    env = load(tmp_path, "A=\"x # y\"\nB='q'\n")
    assert env == {"A": "x # y", "B": "q"}


def test_inline_comment_on_unquoted_value(tmp_path):
    assert load(tmp_path, "A=1   # the port\nB=a#b\n") == {"A": "1", "B": "a#b"}


def test_value_may_contain_equals(tmp_path):
    assert load(tmp_path, "PRINTAPI_PLANS=[{\"id\":\"a=b\"}]\n") == {
        "PRINTAPI_PLANS": "[{\"id\":\"a=b\"}]"}


def test_empty_value(tmp_path):
    assert load(tmp_path, "A=\n") == {"A": ""}


def test_real_environment_wins(tmp_path):
    # A token set on the command line / by docker must not be silently replaced by a stale file.
    assert load(tmp_path, "A=file\nB=file\n", env={"A": "shell"}) == {"A": "shell", "B": "file"}


def test_lines_without_equals_are_ignored(tmp_path):
    assert load(tmp_path, "garbage\nA=1\n") == {"A": "1"}


def test_missing_file_is_a_noop(tmp_path):
    env = {}
    envfile.load(tmp_path / "nope.env", env)
    assert env == {}
