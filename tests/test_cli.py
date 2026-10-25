import pytest

from agents_webhook.cli import main

SUBCOMMANDS = ["migrate", "ingest", "api", "worker"]


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_subcommands_not_wired(name, capsys):
    assert main([name]) == 2
    assert "not wired" in capsys.readouterr().err
