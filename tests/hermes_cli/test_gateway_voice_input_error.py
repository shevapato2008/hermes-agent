"""Classic gateway chat surfaces terminal microphone failures."""

from hermes_cli.gateway_chat_view import GatewayChatView


def test_classic_gateway_chat_prints_voice_input_error(capsys):
    view = object.__new__(GatewayChatView)
    view.quiet = False

    view._dispatch_event(
        "voice.input_error", None,
        {"message": "microphone reached EOF", "stage": "read"},
    )

    assert "voice error: microphone reached EOF" in capsys.readouterr().err
