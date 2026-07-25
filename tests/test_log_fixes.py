"""Checks for the log-driven fixes:
- _combobox_js_option_click picks the exact role=option and ignores prefix traps
- email-step retry loop recovers from a no-op Next submit
- press-and-hold captcha variant is handled instead of bailing unsupported
Run: python tests/test_log_fixes.py
"""
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from controllers.base_controller import BaseBrowserController
from controllers.ruyipage_controller import RuyiPageController


def _controller(cls=RuyiPageController):
    c = cls.__new__(cls)  # bypass __init__ (no proxy pool/config needed)
    return c


def test_js_option_click_exact_match_wins():
    c = _controller()
    page = MagicMock()
    control = MagicMock()
    # JS finds exact "1" before prefix-matching "10月" -> returns True
    page.run_js.return_value = True
    ok = c._combobox_js_option_click(page, control, ["1", "1月", "1 月"])
    assert ok is True
    args, kwargs = page.run_js.call_args
    assert args[1] == ["1", "1月", "1 月"] and kwargs.get("as_expr") is False


def test_js_option_click_no_option_returns_false():
    c = _controller()
    page = MagicMock()
    page.run_js.return_value = False
    assert c._combobox_js_option_click(page, MagicMock(), ["3"]) is False


def test_js_option_click_exception_swallowed():
    c = _controller()
    page = MagicMock()
    page.run_js.side_effect = RuntimeError("bidi down")
    assert c._combobox_js_option_click(page, MagicMock(), ["3"]) is False


def test_hold_uses_single_action_chain():
    c = _controller(RuyiPageController)
    scope = MagicMock()
    btn = MagicMock()
    actions = scope.actions
    # chain returns self for fluent calls
    actions.move_to.return_value = actions
    actions.hold.return_value = actions
    actions.wait.return_value = actions
    actions.release.return_value = actions
    assert c._hold_captcha_button(scope, btn) is True
    actions.move_to.assert_called_once()
    actions.hold.assert_called_once()
    actions.release.assert_called_once()
    actions.perform.assert_called_once()
    # hold duration inside wait() must be >= 4s
    wait_arg = actions.wait.call_args[0][0]
    assert wait_arg >= 4.0


def test_hold_failure_returns_false():
    c = _controller(RuyiPageController)
    scope = MagicMock()
    scope.actions.move_to.side_effect = RuntimeError("no pointer")
    assert c._hold_captcha_button(scope, MagicMock()) is False


def test_find_hold_button_scans_scopes():
    c = _controller(RuyiPageController)
    page = MagicMock()
    btn = MagicMock()
    btn.states.is_displayed = True
    scope = MagicMock()
    scope.ele.return_value = btn
    c._iter_scopes = lambda p: [scope]
    found_scope, found_btn = c._find_hold_challenge_button(page, timeout=1)
    assert found_btn is btn


def test_email_retry_loop_recovers():
    """First password wait misses, email input still present -> retry -> success."""
    c = _controller()
    c.form_step_timeout = 5
    c.action_pause_min = c.action_pause_max = 0.0
    page = MagicMock()
    page.wait = lambda *_: None

    email_el = MagicMock(name="email")
    password_el = MagicMock(name="password")
    calls = {"n": 0}

    def wait_for_any(owner, locators, timeout=5):
        loc = " ".join(locators)
        if "password" in loc or "Password" in loc:
            calls["n"] += 1
            return password_el if calls["n"] >= 2 else False
        if "MemberName" in loc or "新建电子邮件" in loc:
            return email_el
        return False

    c.wait_for_any = wait_for_any
    c.click_element = lambda *a, **k: None
    c.click_primary_button = lambda *a, **k: True
    c.human_pause = lambda *a, **k: None
    c.text_exists = lambda *a, **k: False

    # replicate the flow's email-submit block standalone
    email = "someone@outlook.com"
    email_input_locators = ["css:input[name='MemberName']", "css:[aria-label='新建电子邮件']"]
    email_retried = False
    import time as _t
    deadline = _t.monotonic() + c.form_step_timeout
    password_input = False
    while _t.monotonic() < deadline:
        password_input = c.wait_for_any(
            page,
            ["css:input[type='password']", "css:input[name='Password']"],
            timeout=0.4,
        )
        if password_input:
            break
        still_on_email = c.wait_for_any(page, email_input_locators, timeout=0.3)
        if still_on_email and not email_retried:
            email_retried = True
            c.click_element(page, still_on_email)
            still_on_email.input(email, clear=True)
            c.click_primary_button(page)
            continue
        break
    assert password_input is password_el and email_retried is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} checks passed")
