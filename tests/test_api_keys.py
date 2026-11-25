import pytest
from pydantic import ValidationError

from hub.app import ApiKeyIn, ChatSettingsIn, GoveeKeyIn

GEMINI = "AIza" + "x" * 35
OPENAI = "sk-proj-" + "Ab3_-" * 30
ANTHROPIC = "sk-ant-api03-" + "Zz9_-" * 18
GOVEE = "1b2c3d4e-0000-4a4a-9b9b-123456789abc"


def msg(exc):
    return exc.value.errors()[0]["msg"].removeprefix("Value error, ")


@pytest.mark.parametrize("pasted", [f"  {GEMINI}\n", f"​{GEMINI} ", f'"{GEMINI}"', f"GEMINI_API_KEY={GEMINI}",
                                    GEMINI[:20] + "\n" + GEMINI[20:]])
def test_pasted_gemini_key_is_cleaned(pasted):
    assert ChatSettingsIn(gemini_key=pasted).gemini_key == GEMINI


def test_valid_keys_pass():
    assert ChatSettingsIn(openai_key=f"Bearer {OPENAI}").openai_key == OPENAI
    assert ApiKeyIn(api_key=ANTHROPIC).api_key == ANTHROPIC
    assert GoveeKeyIn(api_key=f" {GOVEE} ").api_key == GOVEE
    assert ChatSettingsIn(quick="gemini:x").gemini_key is None


@pytest.mark.parametrize("model, field, value, expect", [
    (ChatSettingsIn, "gemini_key", "", "Paste your Gemini key first."),
    (ChatSettingsIn, "openai_key", ANTHROPIC, "That's an Anthropic (Claude) key"),
    (ChatSettingsIn, "openai_key", GEMINI, "That's a Google Gemini key"),
    (ChatSettingsIn, "openai_key", "pk-" + "a" * 40, "starts with “sk-”"),
    (ApiKeyIn, "api_key", OPENAI, "starts with “sk-ant-”"),
    (ChatSettingsIn, "gemini_key", "AIzaShort", "too short"),
    (GoveeKeyIn, "api_key", GOVEE + "é", "characters keys don't use"),
])
def test_wrong_keys_say_why(model, field, value, expect):
    with pytest.raises(ValidationError) as exc:
        model(**{field: value})
    assert expect in msg(exc)
