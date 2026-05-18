from lethe.tools.telegram_tools import _reply_fields, clear_telegram_context, set_last_message_id


def teardown_function():
    clear_telegram_context()


def test_reply_fields_zero_falls_back_to_runtime_target():
    set_last_message_id(3294)

    assert _reply_fields(0) == {
        "reply_to_message_id": 3294,
        "allow_sending_without_reply": True,
    }


def test_reply_fields_none_falls_back_to_runtime_target():
    set_last_message_id(3294)

    assert _reply_fields(None) == {
        "reply_to_message_id": 3294,
        "allow_sending_without_reply": True,
    }


def test_reply_fields_positive_id_uses_explicit_target():
    set_last_message_id(3294)

    assert _reply_fields(123, allow_sending_without_reply=False) == {
        "reply_to_message_id": 123,
        "allow_sending_without_reply": False,
    }


def test_reply_fields_negative_id_disables_reply_threading():
    set_last_message_id(3294)

    assert _reply_fields(-1) == {}


def test_reply_fields_missing_runtime_target_sends_normally():
    clear_telegram_context()

    assert _reply_fields(0) == {}
    assert _reply_fields(None) == {}
