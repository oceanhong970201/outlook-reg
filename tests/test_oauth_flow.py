import unittest
from unittest.mock import Mock, patch

import get_token


class InputElement:
    def __init__(self):
        self.values = []

    def input(self, value, clear=False):
        self.values.append((value, clear))


class Button:
    def __init__(self):
        self.clicks = 0

    def click(self):
        self.clicks += 1


class OAuthFormTests(unittest.TestCase):
    def test_password_page_is_handled_with_registered_password(self):
        password_input = InputElement()
        next_button = Button()

        with patch.object(
            get_token,
            "wait_for_any",
            side_effect=[False, password_input, next_button],
        ):
            action = get_token.handle_oauth2_form(
                object(),
                "user@example.com",
                password="Secret123!",
            )

        self.assertEqual(action, "password")
        self.assertEqual(password_input.values, [("Secret123!", True)])
        self.assertEqual(next_button.clicks, 1)

    def test_confirmation_page_is_clicked_after_other_forms(self):
        confirmation_button = Button()

        with patch.object(
            get_token,
            "wait_for_any",
            side_effect=[False, False, False, confirmation_button],
        ):
            action = get_token.handle_oauth2_form(
                object(),
                "user@example.com",
                password=None,
            )

        self.assertEqual(action, "confirmation")
        self.assertEqual(confirmation_button.clicks, 1)


class OAuthDiagnosticsTests(unittest.TestCase):
    def test_retry_logs_typed_exception_diagnostics(self):
        with (
            patch.object(
                get_token,
                "_try_get_access_token",
                side_effect=RuntimeError("listener failed"),
            ),
            patch("builtins.print") as print_mock,
        ):
            result = get_token.get_access_token(object(), "name", max_retries=1)

        self.assertEqual(result, (False, False, False))
        output = "\n".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("[Metric: OAuth]", output)
        self.assertIn('"failure_class": "runtime_error"', output)
        self.assertIn('"stage": "oauth_attempt"', output)

    def test_token_exchange_failure_does_not_retry_whole_authorize_cycle(self):
        # ponytail: an exchange-stage failure (code captured, token endpoint
        # rejected it) must NOT retry the whole authorize → consent → captcha
        # cycle. Re-navigating forces a fresh captcha on a session Microsoft
        # just risk-scored — wasted behavior. Only authorization-stage failures
        # (no code yet) deserve a retry.
        calls = {"count": 0}

        def fake_try(_page, _email, proxy_config=None, password=None):
            calls["count"] += 1
            # Simulate a token_exchange HTTP 400 failure: code was captured but
            # the exchange was rejected. _try_get_access_token routes this
            # through _oauth_failure("token_exchange", ...).
            return get_token._oauth_failure(
                "token_exchange", "token_http_error", status_code=400
            )

        with patch.object(get_token, "_try_get_access_token", side_effect=fake_try):
            result = get_token.get_access_token(object(), "name", max_retries=3)

        self.assertEqual(result, (False, False, False))
        self.assertEqual(calls["count"], 1, "exchange failure must not retry")

    def test_callback_timeout_failure_still_retries(self):
        # authorization-stage failure (no code yet) -> retry is legitimate.
        calls = {"count": 0}

        def fake_try(_page, _email, proxy_config=None, password=None):
            calls["count"] += 1
            return get_token._oauth_failure(
                "authorization_callback", "callback_timeout"
            )

        with patch.object(get_token, "_try_get_access_token", side_effect=fake_try):
            result = get_token.get_access_token(object(), "name", max_retries=3)

        self.assertEqual(result, (False, False, False))
        self.assertEqual(calls["count"], 3, "callback_timeout should retry up to max")

    def test_token_http_error_logs_status_without_response_body(self):
        page = Mock()
        page.listen.listening = True
        page.listen.wait.return_value = None
        page.url = "http://localhost:8000/?code=auth-code"
        response = Mock(status_code=429, text="secret response body")
        response.raise_for_status.side_effect = get_token.requests.HTTPError(
            "429 Client Error", response=response
        )

        config = {
            "email_suffix": "@example.test",
            "oauth2": {
                "Scopes": ["offline_access"],
                "client_id": "client-id",
                "redirect_url": "http://localhost:8000",
                "authorization_timeout": 5,
                "token_timeout": 5,
            },
        }
        with (
            patch.object(get_token, "get_config", return_value=config),
            patch.object(get_token.requests, "post", return_value=response),
            patch("builtins.print") as print_mock,
        ):
            result = get_token._try_get_access_token(page, "name")

        self.assertEqual(result, (False, False, False))
        output = "\n".join(str(call) for call in print_mock.call_args_list)
        self.assertIn('"failure_class": "token_http_error"', output)
        self.assertIn('"status_code": 429', output)
        self.assertNotIn("secret response body", output)


if __name__ == "__main__":
    unittest.main()
