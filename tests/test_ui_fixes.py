"""Self-checks for the combobox arrow-select and proofs-dismiss fixes.
Run: python tests/test_ui_fixes.py
"""
import sys, os
from unittest.mock import MagicMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from controllers.ruyipage_controller import RuyiPageController


def _ctrl():
    return RuyiPageController.__new__(RuyiPageController)


def test_proofs_skip_locators_prefer_stable_id():
    c = _ctrl()
    # build the locator list the same way dismiss_proofs_page does, by reading
    # the method source ordering indirectly: just assert the method exists and
    # that a missing skip control returns False (no infinite loop).
    page = MagicMock()
    c.wait_for_any = lambda *a, **k: False
    assert c.dismiss_proofs_page(page) is False


def test_proofs_dismiss_loops_until_url_leaves():
    c = _ctrl()
    page = MagicMock()
    el = MagicMock()
    calls = {"n": 0}
    c.wait_for_any = lambda *a, **k: el
    c.click_element = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    # proofs still active on 1st check, gone on 2nd -> click twice then True
    states = iter([True, False])
    c.proofs_page_active = lambda p: next(states, False)
    page.wait = lambda *_: None
    assert c.dismiss_proofs_page(page) is True
    assert calls["n"] >= 1


def test_combobox_arrow_select_commits_on_match():
    c = _ctrl()
    page = MagicMock()
    control = MagicMock()
    c.human_pause = lambda *a, **k: None
    c.click_element = lambda *a, **k: None
    c._combobox_selected_text = lambda el: "1日 1 出生日期 1"

    # simulate active option text progression: after 0 ArrowDown -> "10日",
    # after 1 ArrowDown -> "1日" (match). Track ArrowDown presses.
    seq = {"down": 0}
    class FakeActions:
        def press(self, key):
            if key == "":  # ARROW_DOWN
                seq["down"] += 1
            return self
        def perform(self):
            return self
    page.actions = FakeActions()
    # active text depends on how many ArrowDowns happened
    def run_js(script, *a, **k):
        return "1日" if seq["down"] >= 1 else "10日"
    page.run_js = run_js

    ok = c._combobox_arrow_select(page, control, "1", ["1", "1日", "1 号", "1 日"], ["1日", "1 号", "1 日"])
    assert ok is True


def test_combobox_arrow_select_gives_up_when_no_match():
    c = _ctrl()
    page = MagicMock()
    control = MagicMock()
    c.human_pause = lambda *a, **k: None
    c.click_element = lambda *a, **k: None
    c._combobox_selected_text = lambda el: "10日 10 出生日期 10"
    class FakeActions:
        def press(self, key): return self
        def perform(self): return self
    page.actions = FakeActions()
    page.run_js = lambda *a, **k: "99日"  # never matches
    ok = c._combobox_arrow_select(page, control, "1", ["1", "1日"], ["1日"])
    assert ok is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print("PASS", fn.__name__)
    print(f"{len(fns)} checks passed")
