import random
import time

from ruyipage import FirefoxOptions, FirefoxPage

from config import get_config
from flow_types import RegistrationOutcome
from .base_controller import BaseBrowserController

class RuyiPageController(BaseBrowserController):
    def __init__(self, proxy_pool=None):
        super().__init__(proxy_pool=proxy_pool)
        data = get_config()

        controller_config = data.get("ruyipage", {})
        self.browser_path = controller_config.get("browser_path", "").strip()
        self.headless = bool(controller_config.get("headless", False))
        self.xpath_picker = bool(controller_config.get("xpath_picker", False))
        self.action_visual = bool(controller_config.get("action_visual", False))
        self.block_images = bool(controller_config.get("block_images", False))
        self.javascript_memory_limit_mb = max(
            0, int(controller_config.get("javascript_memory_limit_mb", 0) or 0)
        )
        self.challenge_wait_timeout = max(
            1.0, float(data.get("challenge_wait_timeout", 15))
        )

    def launch_browser(self, proxy_config=None):
        profile_dir = None

        try:
            profile_dir = self.build_profile_dir()
            opts = FirefoxOptions()
            opts.close_on_exit(True)
            opts.set_load_mode("eager")
            opts.set_timeouts(base=5, page_load=40, script=10)
            # ponytail: under memory pressure (only ~2.9GB free on this box)
            # the BiDi remote-debugging port can take >22s to come up, which is
            # ruyipage's default connect budget (retry 10 × 2s). That tripped
            # the "首次启动连接失败" restart loop in the logs. Give the first
            # connect ~45s so slow-but-healthy launches stop dying.
            opts.set_retry(times=15, interval=3.0)
            opts.set_pref("intl.accept_languages", "zh-CN,zh,en-US,en")
            opts.set_human_algorithm("windmouse")
            opts.headless(self.headless)
            opts.set_user_dir(profile_dir)

            # 性能优化：减少内存和 CPU 占用
            opts.set_pref("browser.cache.disk.enable", False)  # 禁用磁盘缓存
            opts.set_pref("browser.cache.memory.enable", True)  # 只用内存缓存
            opts.set_pref("browser.cache.memory.capacity", 8192)  # 限制内存缓存 8MB
            opts.set_pref("browser.sessionhistory.max_total_viewers", 0)  # 禁用后退缓存
            opts.set_pref("media.autoplay.default", 5)  # 禁止自动播放媒体
            if self.block_images:
                opts.set_pref("permissions.default.image", 2)
            if self.javascript_memory_limit_mb:
                opts.set_pref(
                    "javascript.options.mem.high_water_mark",
                    self.javascript_memory_limit_mb,
                )

            if self.browser_path:
                opts.set_browser_path(self.browser_path)
            if proxy_config:
                opts.set_proxy(proxy_config["requests_url"])
            if self.xpath_picker:
                opts.enable_xpath_picker(True)
            if self.action_visual:
                opts.enable_action_visual(True)

            page = FirefoxPage(opts)
            try:
                page.set_viewport(800, 600)
            except Exception:
                pass

            # 不要全屏/最大化，保持小窗口
            try:
                page.window.set_window_size(820, 650)
            except Exception:
                pass

            return page, {"page": page, "profile_dir": profile_dir}
        except Exception as e:
            # ponytail: a failed launch can leave a half-started Firefox holding
            # parent.lock on profile_dir, so rmtree fails and the dir becomes a
            # permanent orphan (we accumulated 15). Kill any process listening
            # on the leftover BiDi/debug ports before removing, so the profile
            # dir is actually reclaimable.
            self._kill_orphan_firefox()
            if profile_dir:
                removed = self._remove_profile_dir(profile_dir)
                if not removed:
                    with self.cleanup_lock:
                        self.orphan_profile_dirs.add(profile_dir)
            print(f"启动 ruyipage 浏览器失败: {e}")
            return False, False

    def _kill_orphan_firefox(self):
        """Best-effort kill of ruyipage Firefox processes we launched.

        Only targets processes whose exe path is the configured ruyipage
        browser, never a system/user Firefox. No-op on any error."""
        if not self.browser_path:
            return
        try:
            import os
            import subprocess
            ps = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.ExecutablePath -eq $env:RUYI_FF } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                env={**os.environ, "RUYI_FF": self.browser_path},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception:
            pass

    def _iter_scopes(self, root):
        yield root
        try:
            children = root.get_frames()
        except Exception:
            children = []
        for child in children:
            yield from self._iter_scopes(child)

    def _challenge_active(self, page):
        if self.mailbox_url_ready(page) or self.proofs_page_active(page):
            return False
        return bool(
            self.visible_locator_exists(
                page,
                [
                    "css:iframe#enforcementFrame",
                    "css:iframe[title*='验证']",
                    "css:iframe[src*='arkoselabs']",
                    "css:iframe[src*='funcaptcha']",
                ],
                timeout=0.3,
                min_size=20,
            )
            or self.text_exists(page, "证明你不是机器人")
            or self.text_exists(page, "验证你不是机器人")
            or self.text_exists(page, "长按该按钮")
            or self.text_exists(page, "请再试一次")
            or self.text_exists(page, "Press and hold")
            or self.text_exists(page, "点击以验证")
            or self.text_exists(page, "单击以验证")
            or self.text_exists(page, "可访问性挑战")
            or self.text_exists(page, "再次按下")
            or self.text_exists(page, "Click to verify")
            or self.text_exists(page, "Verify you are human")
        )

    def challenge_active(self, page):
        """Use the ruyipage-aware detector from the base flow.

        The registration state machine calls ``challenge_active`` (without the
        private prefix).  Keeping the private helper as the implementation
        preserves compatibility with older callers while making the richer
        frame/text detection effective for mailbox and proof-page checks.
        """
        return self._challenge_active(page)

    def _challenge_cleared(self, page):
        if self.mailbox_url_ready(page) or self.proofs_page_active(page):
            return True
        return not self._challenge_active(page)

    def _find_captcha_button_in_scopes(self, page, label, timeout=8):
        """
        递归遍历 page 与所有 frame，找到匹配 aria-label 的按钮。
        不依赖固定 iframe 路径，只要按钮被渲染出来就能找到，
        从而绕开 iframe[style*="display: block"] 这种脆弱选择器。
        返回 (scope, button) 或 (None, None)。
        """
        locator = f'css:[aria-label="{label}"]'
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            try:
                scopes = list(self._iter_scopes(page))
            except Exception:
                scopes = [page]

            for scope in scopes:
                try:
                    btn = scope.ele(locator, timeout=0.15)
                except Exception:
                    btn = None
                if not btn:
                    continue
                try:
                    if not btn.states.is_displayed:
                        continue
                except Exception:
                    pass
                try:
                    x, y = btn.rect.viewport_midpoint
                    if not x or not y or x <= 0 or y <= 0:
                        continue
                except Exception:
                    continue
                return scope, btn

            try:
                page.wait(0.25)
            except Exception:
                time.sleep(0.25)
        return None, None

    def _click_captcha_button(self, scope, element, jitter_x=10, jitter_y=10):
        """
        点击挑战按钮：
        - 优先用 scope.actions.move_to(element)：让 ruyipage 自动处理 frame 偏移
          （对比之前用 page.actions.move_to({"x":..,"y":..}) 的坐标在 iframe 里是错的）
        - 失败再回退到 element.click() 这条 BiDi-trusted 路径
        """
        try:
            scope.actions.move_to(
                element,
                offset_x=random.randint(-jitter_x, jitter_x),
                offset_y=random.randint(-jitter_y, jitter_y),
                duration=random.randint(80, 180),
            ).click().perform()
            return True
        except Exception as e1:
            print(f"[Debug: Captcha] - actions.move_to(element) 点击失败，回退 element.click(): {e1}")
            try:
                element.click()
                return True
            except Exception as e2:
                print(f"[Debug: Captcha] - element.click() 也失败: {e2}")
                return False

    def _hold_captcha_button(self, scope, element, hold_seconds=8):
        """Press-and-hold on the 长按该按钮 challenge variant.

        The accessibility-press flow only knows 可访问性挑战/再次按下; the
        press-and-hold widget needs a real pointerDown held for several
        seconds then pointerUp (Arkose measures hold duration). Use the
        scope's own action chain so frame offsets are handled by ruyipage,
        and keep the down/up in ONE perform() so the browser never sees a
        released pointer between them."""
        try:
            hold_for = max(4.0, float(hold_seconds)) + random.uniform(0.4, 1.2)
            scope.actions.move_to(
                element,
                offset_x=random.randint(-6, 6),
                offset_y=random.randint(-4, 4),
                duration=random.randint(120, 220),
            ).hold().wait(hold_for).release().perform()
            return True
        except Exception as e:
            print(f"[Debug: Captcha] - press-and-hold 执行失败: {e}")
            return False

    def _find_hold_challenge_button(self, page, timeout=6):
        """Locate the press-and-hold challenge button across page + frames.

        The widget is usually a role=button / button / [aria-label] whose
        text or label mentions 长按 / Press and hold. Returns (scope, btn)."""
        locators = [
            "xpath://button[contains(normalize-space(.), '长按') or contains(@aria-label, '长按')]",
            "xpath://*[@role='button'][contains(normalize-space(.), '长按') or contains(@aria-label, '长按')]",
            "xpath://button[contains(normalize-space(.), 'Press and hold') or contains(@aria-label, 'Press and hold')]",
            "xpath://*[@role='button'][contains(normalize-space(.), 'Press and hold') or contains(@aria-label, 'Press and hold')]",
            "xpath://*[contains(@aria-label, '按住') or contains(@aria-label, 'hold')][@role='button' or self::button]",
            # Arkose press-and-hold widget: the visible 长按该按钮 label sits on
            # a generic element next to a large pressable target, not always a
            # <button>. Catch any element whose own text/label is the hint.
            "xpath://*[normalize-space(.)='长按该按钮' or @aria-label='长按该按钮']",
            "xpath://*[normalize-space(.)='Press and hold' or @aria-label='Press and hold']",
        ]
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            try:
                scopes = list(self._iter_scopes(page))
            except Exception:
                scopes = [page]
            for scope in scopes:
                for locator in locators:
                    try:
                        btn = scope.ele(locator, timeout=0.15)
                    except Exception:
                        btn = None
                    if not btn:
                        continue
                    try:
                        if not btn.states.is_displayed:
                            continue
                    except Exception:
                        pass
                    return scope, btn
            try:
                page.wait(0.25)
            except Exception:
                time.sleep(0.25)
        return None, None

    def handle_captcha(self, page):
        """
        处理验证码挑战 - 镜像 patchright 工作版本：
        1. 在所有 frame 中递归找 "可访问性挑战" 按钮（不再依赖具体 iframe 路径）
        2. 点击它，然后点击 "再次按下"
        3. 等待 .draw detach（验证通过的关键信号）
        4. 检查加载/风控/重试状态
        若按钮连续 3 次都未找到，则直接判定失败，由上层关闭浏览器、进入下一线程
        """
        print("[Info: Captcha] - 开始处理验证码...")
        self._clear_application_outcome_hint()

        def fail(detail, outcome=RegistrationOutcome.CAPTCHA_FAILED):
            self._set_application_outcome_hint(outcome, detail)
            return False

        # 连续未找到 "可访问性挑战" 的次数；达到阈值即直接失败
        not_found_streak = 0
        not_found_limit = 3

        try:
            for attempt in range(self.max_captcha_retries + 1):
                page.wait(0.3)

                if self._challenge_cleared(page):
                    return True

                # 1. 在所有 scope 中找 "可访问性挑战" 按钮
                scope, btn = self._find_captcha_button_in_scopes(
                    page, "可访问性挑战", timeout=12
                )
                if not btn:
                    if self._challenge_cleared(page):
                        print("[Info: Captcha] - 未发现挑战按钮且无挑战状态，判定通过")
                        return True

                    # Press-and-hold variant (长按该按钮 / Press and hold).
                    # flow-28776-000015 died here: the old code only knew the
                    # accessibility-press buttons and bailed as "unsupported".
                    hold_scope, hold_btn = self._find_hold_challenge_button(
                        page, timeout=2
                    )
                    if hold_btn:
                        print("[Info: Captcha] - 检测到长按验证，执行 press-and-hold")
                        if not self._hold_captcha_button(hold_scope, hold_btn):
                            return fail("press-and-hold interaction failed")
                        page.wait(2.0)
                        if self._challenge_cleared(page):
                            return True
                        if self.error_text_detected(page):
                            return fail(
                                "risk message detected after press-and-hold",
                                RegistrationOutcome.APPLICATION_RISK,
                            )
                        continue

                    # ponytail: _challenge_active 认 Click to verify / Verify you are human /
                    # 长按该按钮 等，但本 handler 只能点 可访问性挑战 / 再次按下。命中不支援
                    # 变种（本质是 IP 风控升级）直接 bail，省下 3×~12s 无意义轮询。
                    if (
                        not self.text_exists(page, "可访问性挑战")
                        and not self.text_exists(page, "再次按下")
                    ):
                        print("[Error: Captcha] - 不支援的挑战变体（IP 风控升级），快速放弃")
                        return fail("unsupported challenge variant")

                    not_found_streak += 1
                    print(
                        f"[Info: Captcha] - 第 {not_found_streak}/{not_found_limit} 次"
                        f"未找到 可访问性挑战 按钮"
                    )
                    if not_found_streak >= not_found_limit:
                        print(
                            "[Error: Captcha] - 连续 "
                            f"{not_found_limit} 次未找到挑战按钮，判定失败，"
                            "关闭浏览器并继续下一个线程"
                        )
                        return fail("challenge button not found after repeated attempts")
                    # 短暂等待后再尝试
                    page.wait(1.0)
                    continue

                # 找到按钮则重置未找到计数
                not_found_streak = 0

                # 2. 点击 "可访问性挑战"
                print("[Debug: Captcha] - 点击 [aria-label='可访问性挑战']")
                if not self._click_captcha_button(scope, btn, jitter_x=10, jitter_y=10):
                    return fail("challenge button click failed")
                time.sleep(random.uniform(0.2, 0.45))

                # 3. 点击 "再次按下"（先在同 scope 找，找不到再全局递归）
                btn2 = None
                btn2_scope = scope
                try:
                    btn2 = scope.ele('css:[aria-label="再次按下"]', timeout=3)
                except Exception:
                    btn2 = None
                if not btn2:
                    btn2_scope, btn2 = self._find_captcha_button_in_scopes(
                        page, "再次按下", timeout=5
                    )
                if btn2:
                    print("[Debug: Captcha] - 点击 [aria-label='再次按下']")
                    if not self._click_captcha_button(
                        btn2_scope, btn2, jitter_x=20, jitter_y=13
                    ):
                        return fail("challenge confirmation click failed")

                # 4. 等待 .draw 消失（patchright 的关键判定信号）
                try:
                    page.wait.ele_deleted(
                        'css:.draw', timeout=self.challenge_wait_timeout
                    )
                except Exception:
                    if self.text_exists(page, '取消') or self._challenge_cleared(page):
                        return True
                    print(f"[Info: Captcha] - 第 {attempt + 1} 次：.draw 未消失，继续重试")
                    continue

                # 5. 等加载/检查风控/检查是否仍有挑战
                try:
                    loading = page.ele(
                        'css:[role="status"][aria-label="正在加载..."]', timeout=5
                    )
                    if loading:
                        page.wait(8)

                    if self.service_unavailable_detected(page):
                        return fail(
                            "service unavailable after captcha",
                            RegistrationOutcome.SERVICE_UNAVAILABLE,
                        )

                    if self.error_text_detected(page):
                        print("[Error: Rate limit] - 正常通过验证码，但当前 IP 注册频率过快。")
                        return fail(
                            "risk message detected after captcha",
                            RegistrationOutcome.APPLICATION_RISK,
                        )

                    if self._challenge_cleared(page):
                        return True

                    _, still = self._find_captcha_button_in_scopes(
                        page, "可访问性挑战", timeout=1
                    )
                    if still:
                        print(f"[Info: Captcha] - 第 {attempt + 1} 次未通过，继续重试")
                        continue
                    return True
                except Exception:
                    if self.text_exists(page, '取消') or self._challenge_cleared(page):
                        return True
                    continue

            print("[Error: Captcha] - 重试次数已达到上限，验证码处理失败。")
            return fail("captcha retry limit reached")

        except Exception as e:
            print(f"[Debug: Captcha] - 简易验证码处理逻辑出现异常: {e}")
            transport_class = self._transport_failure_class(e)
            if transport_class:
                return fail(
                    f"{transport_class}: {e}",
                    RegistrationOutcome.TRANSPORT_ERROR,
                )
            return fail(
                f"captcha handler exception: {type(e).__name__}: {e}",
                RegistrationOutcome.UI_ERROR,
            )
