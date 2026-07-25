import builtins
import ctypes
import inspect
import itertools
import json
import math
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime

from config import get_config, update_config, save_config
from controllers.ruyipage_controller import RuyiPageController
from get_token import get_access_token
from proxy_pool import ManagedProxyPool
from utils import (
    generate_strong_password,
    get_proxy_capacity,
    get_proxy_source_config,
    masked_proxy_id,
    random_email,
)


BANNER_LINES = [
    r"  __  __ ____    __  __    _    ___ _       ____  _____ ____   _____ ___   ___  _   ",
    r" |  \/  / ___|  |  \/  |  / \  |_ _| |     |  _ \| ____/ ___| |_   _/ _ \ / _ \| |  ",
    r" | |\/| \___ \  | |\/| | / _ \  | || |     | |_) |  _|| |  _    | || | | | | | | |  ",
    r" | |  | |___) | | |  | |/ ___ \ | || |___  |  _ <| |__| |_| |   | || |_| | |_| | |__",
    r" |_|  |_|____/  |_|  |_/_/   \_\___|_____| |_| \_\_____\____|   |_| \___/ \___/|____|",
]

DIM = "\033[2m"
RESET = "\033[0m"
CLEAR_LINE = "\033[2K"
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_flow_counter = itertools.count(1)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

try:
    import unicodedata as _unicodedata
except Exception:  # pragma: no cover
    _unicodedata = None


def _display_width(text):
    """Terminal cells a string occupies, counting CJK/fullwidth chars as 2.
    ponytail: len() under-counts '总任务/剩余' (each char is 2 cells), so a
    status line that fits len() still wraps and leaves residue that the inline
    redraw (\r + 2K, single line only) cannot erase — the screen fills with
    stacked status lines. Cell-accurate width fixes that."""
    import unicodedata
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
            width += 2
        else:
            width += 1
    return width


def _truncate_to_width(text, max_width):
    """Truncate `text` (plain, no ANSI) so it occupies at most max_width
    terminal cells, on a character boundary that does not split a wide char."""
    if max_width <= 0:
        return ""
    import unicodedata
    width = 0
    out = []
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
        if width + cw > max_width:
            break
        out.append(ch)
        width += cw
    return "".join(out)


def _term_truncate(msg, cols):
    """Cap each line of msg to one terminal row so it cannot auto-wrap.
    A wrapped line leaves residue the inline status redraw (\r + single-row
    2K) cannot erase. ANSI escapes don't consume cell width, so strip them
    before measuring but preserve them in the output."""
    if cols <= 1:
        return msg
    lines = msg.split("\n")
    capped = []
    for line in lines:
        plain = _ANSI_ESCAPE_RE.sub("", line)
        if _display_width(plain) <= cols:
            capped.append(line)
        else:
            # ponytail: preserve leading color escape (if any) on the truncated
            # line so a colored line stays colored up to its cut.
            head = ""
            match = re.match(r"^(\x1b\[[0-9;]*m)", line)
            if match:
                head = match.group(1)
            capped.append(head + _truncate_to_width(plain, cols))
    return "\n".join(capped)


def enable_windows_vt():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)


# ── smooth flowing color ──────────────────────────────────────────────

def _status_color(pos, t):
    """Cyan-blue gradient for status bar."""
    phase = pos * 0.3 - t
    r = int(30 + 20 * math.sin(phase + 2.0))
    g = int(180 + 75 * math.sin(phase + 0.5))
    b = int(220 + 35 * math.sin(phase))
    return f"\033[38;2;{max(0,min(255,r))};{max(0,min(255,g))};{max(0,min(255,b))}m"


def _banner_color(pos, t):
    """Purple-magenta to deep-blue gradient for banner."""
    phase = pos * 0.15 - t * 0.8
    r = int(160 + 80 * math.sin(phase + 0.8))
    g = int(30 + 40 * math.sin(phase + 3.0))
    b = int(180 + 70 * math.sin(phase))
    return f"\033[38;2;{max(0,min(255,r))};{max(0,min(255,g))};{max(0,min(255,b))}m"


def _flow_text(text, t, color_fn):
    out = []
    ci = 0
    for ch in text:
        if ch == ' ':
            out.append(ch)
        else:
            out.append(f"{color_fn(ci, t)}{ch}")
            ci += 1
    out.append(RESET)
    return "".join(out)


# ── status bar ────────────────────────────────────────────────────────

_tls = threading.local()


class StatusBar:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        self._time = 0.0
        self._started_at = None
        self._finished_at = None
        self.show_logs = True
        self.save_logs = False
        self.log_path = None
        self.total_tasks = 0
        self.submitted = 0
        self.active_threads = 0
        self.succeeded = 0
        self.failed = 0
        self.oauth_succeeded = 0
        self.oauth_failed = 0
        self.enable_oauth2 = False
        self.stopping = False
        self._original_print = builtins.print
        self._original_stdout_write = None
        self._original_stderr_write = None
        self._refresh_thread = None
        self._interactive = False
        self._noninteractive_filter = False
        self._refresh_interval = 0.2
        self._success_messages = []
        self._max_success_lines = 0
        self._log_file = None

    def configure_log_file(self, enabled):
        self.save_logs = bool(enabled)
        if not self.save_logs:
            self.log_path = None

    def _raw(self, data):
        _tls.in_raw = True
        try:
            self._original_stdout_write(data)
        finally:
            _tls.in_raw = False

    def _term_size(self):
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except (OSError, ValueError):
            return 80, 24

    def _open_log_file(self):
        if not self.save_logs or self._log_file is not None:
            return
        logs_dir = os.path.join(PROJECT_ROOT, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.log_path = os.path.join(logs_dir, f"run_{timestamp}_{os.getpid()}.log")
        self._log_file = open(self.log_path, "x", encoding="utf-8", newline="\n")

    def _write_log_file(self, message):
        if self._log_file is None or not message:
            return
        clean = _ANSI_ESCAPE_RE.sub("", str(message)).replace("\r", "")
        if not clean:
            return
        self._log_file.write(clean)

    def close_log_file(self):
        with self._lock:
            if self._log_file is None:
                return self.log_path
            self._log_file.flush()
            self._log_file.close()
            self._log_file = None
            return self.log_path

    def start(self):
        self._interactive = bool(
            getattr(sys.stdout, "isatty", lambda: False)()
        )
        self._original_stdout_write = sys.stdout.write
        self._original_stderr_write = sys.stderr.write
        self._started_at = time.monotonic()
        self._finished_at = None
        self._active = True
        self._open_log_file()

        if not self._interactive:
            if not self.show_logs or self.save_logs:
                self._noninteractive_filter = True
                self._patch_print()
            return

        config = get_config()
        self._refresh_interval = max(
            0.05, float(config.get("status_refresh_interval", 0.2))
        )
        self._patch_print()
        self._patch_streams()
        if self.show_logs:
            w, _ = self._term_size()
            self._raw(f"\n{DIM}{'─' * w}{RESET}\n")
            self._draw_status_inline()
        else:
            self._init_quiet_screen()
        self._refresh_thread = threading.Thread(target=self._animation_loop, daemon=True)
        self._refresh_thread.start()

    def stop(self):
        self._active = False
        self._finished_at = time.monotonic()
        if not self._interactive:
            if self._noninteractive_filter:
                self._restore_print()
                self._noninteractive_filter = False
            self.close_log_file()
            return
        if self._refresh_thread:
            self._refresh_thread.join(timeout=0.5)
        self._restore_streams()
        self._restore_print()
        with self._lock:
            if self.show_logs:
                self._raw("\r" + CLEAR_LINE + "\n")
            else:
                self._raw("\033[?25h\n")
        self.close_log_file()

    def _patch_print(self):
        bar = self

        def patched_print(*args, **kwargs):
            file = kwargs.get("file", None)
            if file is not None and file is not sys.stdout and file is not sys.stderr:
                bar._original_print(*args, **kwargs)
                return
            end = kwargs.get("end", "\n")
            sep = kwargs.get("sep", " ")
            msg = sep.join(str(a) for a in args) + end
            bar.log(msg)

        builtins.print = patched_print

    def _restore_print(self):
        builtins.print = self._original_print

    def _patch_streams(self):
        bar = self

        def make_patched(original_write):
            def patched_write(data):
                if getattr(_tls, 'in_raw', False) or not bar._active or not data:
                    return original_write(data)
                bar.log(data)
                return len(data)
            return patched_write

        sys.stdout.write = make_patched(self._original_stdout_write)
        sys.stderr.write = make_patched(self._original_stderr_write)

    def _restore_streams(self):
        if self._original_stdout_write:
            sys.stdout.write = self._original_stdout_write
        if self._original_stderr_write:
            sys.stderr.write = self._original_stderr_write

    def log(self, msg):
        with self._lock:
            self._write_log_file(msg)
        if not self._active:
            self._original_print(msg, end="" if msg.endswith("\n") else "\n")
            return
        if not self._interactive and not self.show_logs:
            if "[Success" in msg:
                self._original_print(msg, end="" if msg.endswith("\n") else "\n")
            return
        if self.show_logs:
            with self._lock:
                try:
                    cols, _ = self._term_size()
                except Exception:
                    cols = 80
                display = _term_truncate(msg, cols - 1)
                self._raw("\r" + CLEAR_LINE)
                self._raw(display)
                if not display.endswith("\n"):
                    self._raw("\n")
                self._draw_status_inline()
        else:
            if "[Success" not in msg:
                return
            with self._lock:
                line = msg.rstrip("\n")
                self._success_messages.append(line)
                if len(self._success_messages) > self._max_success_lines:
                    self._success_messages = self._success_messages[-self._max_success_lines:]
                self._redraw_quiet_screen()

    def refresh(self):
        if not self._active:
            return
        with self._lock:
            if self.show_logs:
                self._draw_status_inline()
            else:
                self._redraw_quiet_screen()

    def _animation_loop(self):
        while self._active:
            time.sleep(self._refresh_interval)
            self._time += self._refresh_interval * 3
            self.refresh()

    # ── log mode (Y) ──────────────────────────────────────────────────

    def _draw_status_inline(self):
        line = self._build_status_line()
        self._raw("\r" + CLEAR_LINE + " " + line)

    # ── quiet mode (N) ────────────────────────────────────────────────

    def _init_quiet_screen(self):
        cols, lines = self._term_size()
        self._max_success_lines = max(1, lines - len(BANNER_LINES) - 5)
        self._raw("\033[?25l")
        self._raw("\033[2J\033[H")
        self._redraw_quiet_screen()

    def _redraw_quiet_screen(self):
        cols, lines = self._term_size()
        self._max_success_lines = max(1, lines - len(BANNER_LINES) - 5)

        out = ["\033[H\033[J"]
        for i, raw_line in enumerate(BANNER_LINES):
            colored = _flow_text(raw_line, self._time + i * 0.6, _banner_color)
            out.append(colored + "\n")

        out.append(DIM + "─" * cols + RESET + "\n")

        green = "\033[38;2;120;230;160m"
        for line in self._success_messages[-self._max_success_lines:]:
            out.append(green + line + RESET + "\n")

        out.append(f"\033[{lines};1H")
        out.append("\033[2K " + self._build_status_line())

        self._raw("".join(out))

    # ── status line builder ───────────────────────────────────────────

    def _build_status_line(self):
        remaining = max(0, self.total_tasks - self.submitted)
        total_done = self.succeeded + self.failed
        rate = (self.succeeded / total_done * 100) if total_done > 0 else 0.0
        elapsed = self.elapsed_seconds()
        throughput = (total_done / elapsed * 60) if elapsed > 0 else 0.0

        parts = [
            f"总任务: {self.total_tasks}",
            f"剩余: {remaining}",
            f"运行中: {self.active_threads}",
            f"成功: {self.succeeded}",
            f"失败: {self.failed}",
            f"成功率: {rate:.1f}%",
            f"吞吐: {throughput:.1f}/分钟",
        ]
        if self.enable_oauth2:
            parts.append(f"授权成功: {self.oauth_succeeded}")
            parts.append(f"授权失败: {self.oauth_failed}")
        if self.stopping:
            parts.append("[正在停止...]")

        plain = "  │  ".join(parts)
        # ponytail: cell-accurate truncation (CJK counts 2 cells). A line wider
        # than the terminal wraps, and the \r + single-line 2K redraw cannot
        # erase the wrapped remainder — the status line then stacks across the
        # whole screen. Keep one cell of slack for the leading space + cursor.
        try:
            cols, _ = self._term_size()
        except Exception:
            cols = 80
        plain = _truncate_to_width(plain, max(1, cols - 2))
        return _flow_text(plain, self._time, _status_color)

    def elapsed_seconds(self):
        if self._started_at is None:
            return 0.0
        end_time = self._finished_at or time.monotonic()
        return max(0.0, end_time - self._started_at)


status_bar = StatusBar()
stop_event = threading.Event()
_signal_count = 0
_token_write_lock = threading.Lock()


def signal_handler(sig, frame):
    global _signal_count
    _signal_count += 1
    if _signal_count == 1:
        stop_event.set()
        status_bar.stopping = True
        status_bar.refresh()
    else:
        status_bar.stop()
        sys.stdout.write("\n[Warn] - 再次收到中断信号，强制退出\n")
        os._exit(1)


# ── interactive prompts ───────────────────────────────────────────────


def prompt_choice(prompt, choices, default=None):
    while True:
        suffix = f" (默认: {default})" if default else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw in choices:
            return raw
        print(f"  无效输入，请输入 {'/'.join(choices)}")


def prompt_int(prompt, default, min_value=1):
    while True:
        raw = input(f"{prompt} (默认: {default}): ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value < min_value:
                print(f"  数值必须 >= {min_value}")
                continue
            return value
        except ValueError:
            print("  请输入整数")


def prompt_yes_no(prompt, default=True):
    default_str = "y" if default else "n"
    while True:
        raw = input(f"{prompt} (y/n, 默认: {default_str}): ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  请输入 y 或 n")


def prompt_str(prompt, default=""):
    display = default if default else "无"
    raw = input(f"{prompt} (默认: {display}): ").strip()
    if not raw:
        return default
    return raw


def collect_user_inputs():
    cfg = get_config()
    oauth2_cfg = cfg.get("oauth2", {})

    print("\n================================================================================\n")
    for line in BANNER_LINES:
        print(line)
    print("\n================================================================================\n")
    print("  欢迎使用 MS MAIL REG TOOL，请按提示输入配置参数：")
    print("  （直接回车使用上次保存的值）\n")

    # [1] 邮箱后缀
    print("  [1] 邮箱后缀:")
    print("      1. @outlook.com")
    print("      2. @hotmail.com")
    saved_suffix = cfg.get("email_suffix", "@outlook.com")
    default_suffix_choice = "2" if saved_suffix == "@hotmail.com" else "1"
    suffix_choice = prompt_choice("      请选择", ["1", "2"], default=default_suffix_choice)
    email_suffix = "@outlook.com" if suffix_choice == "1" else "@hotmail.com"
    print(f"      -> {email_suffix}\n")

    # [2] 代理来源
    print("  [2] 代理来源:")
    print("      1. file (从文件读取 HOST:PORT:USER:PASS)")
    print("      2. freefile (从文件读取 HOST:PORT)")
    print("      3. api (从 API 获取)")
    print("      4. residential (711 住宅代理，sticky-per-flow 自动换 IP)")
    source_map = {"file": "1", "freefile": "2", "api": "3", "residential": "4"}
    default_proxy_choice = source_map.get(cfg.get("proxy_source", "api"), "3")
    proxy_choice = prompt_choice("      请选择", ["1", "2", "3", "4"], default=default_proxy_choice)
    proxy_source_map = {"1": "file", "2": "freefile", "3": "api", "4": "residential"}
    proxy_source = proxy_source_map[proxy_choice]
    print(f"      -> {proxy_source}\n")

    # [3] 代理文件 / API 地址 / 住宅代理
    proxy_file = cfg.get("proxy_file", "proxies.txt")
    proxy_api_url = cfg.get("proxy_api_url", "")
    residential_host = cfg.get("proxy_residential_host", "")
    residential_port = cfg.get("proxy_residential_port", 10000)
    residential_username = cfg.get("proxy_residential_username", "")
    residential_password = cfg.get("proxy_residential_password", "")
    if proxy_source in ("file", "freefile"):
        print("  [3] 代理文件路径:")
        proxy_file = prompt_str("      请输入", default=proxy_file)
        print(f"      -> {proxy_file}\n")
    elif proxy_source == "residential":
        print("  [3] 711 住宅代理:")
        residential_host = prompt_str("      网关主机 (如 global.xxx.711proxy.com)", default=residential_host)
        residential_port = prompt_int("      端口 (10000=GB / 20000=IP)", default=residential_port)
        residential_username = prompt_str("      用户名 (不含 session 后缀)", default=residential_username)
        residential_password = prompt_str("      密码", default=residential_password)
        print(f"      -> {residential_host}:{residential_port}\n")
    else:
        print("  [3] 代理 API 地址:")
        proxy_api_url = prompt_str("      请输入", default=proxy_api_url)
        print(f"      -> {proxy_api_url}\n")

    # [4] 并发线程数
    print("  [4] 并发线程数:")
    concurrent_flows = prompt_int("      请输入", default=cfg.get("concurrent_flows", 10))
    print(f"      -> {concurrent_flows}\n")

    # [5] 最大任务数
    print("  [5] 最大任务数:")
    max_tasks = prompt_int("      请输入", default=cfg.get("max_tasks", 20))
    print(f"      -> {max_tasks}\n")

    # [6] OAuth2
    print("  [6] 注册成功后是否授权 OAuth2 获取 Token:")
    print("      Y = 授权（需填写 Client ID 和 Redirect URL）")
    print("      N = 不授权，注册成功后直接保存账号密码")
    enable_oauth2 = prompt_yes_no("      请选择", default=oauth2_cfg.get("enable_oauth2", False))
    print(f"      -> {'授权' if enable_oauth2 else '不授权'}\n")

    client_id = oauth2_cfg.get("client_id", "")
    redirect_url = oauth2_cfg.get("redirect_url", "http://localhost:8000")
    if enable_oauth2:
        print("  [6a] OAuth2 Client ID:")
        client_id = prompt_str("       请输入", default=client_id)
        print(f"       -> {client_id}\n")

        print("  [6b] OAuth2 Redirect URL:")
        redirect_url = prompt_str("       请输入", default=redirect_url)
        print(f"       -> {redirect_url}\n")

    # [7] 显示日志
    print("  [7] 是否显示日志:")
    print("      Y = 显示全部日志")
    print("      N = 仅显示注册成功信息（带流光 Banner）")
    show_logs = prompt_yes_no("      请选择", default=True)
    print(f"      -> {'显示全部日志' if show_logs else '仅显示成功信息'}\n")

    # [8] 保存日志
    print("  [8] 任务结束后是否保存日志:")
    print("      Y = 保存完整日志到 logs 目录")
    print("      N = 不保存日志文件")
    save_logs = prompt_yes_no(
        "      请选择",
        default=bool(cfg.get("save_logs", False)),
    )
    print(f"      -> {'保存日志' if save_logs else '不保存日志'}\n")

    return {
        "email_suffix": email_suffix,
        "proxy_source": proxy_source,
        "proxy_file": proxy_file,
        "proxy_api_url": proxy_api_url,
        "residential_host": residential_host,
        "residential_port": residential_port,
        "residential_username": residential_username,
        "residential_password": residential_password,
        "concurrent_flows": concurrent_flows,
        "max_tasks": max_tasks,
        "enable_oauth2": enable_oauth2,
        "client_id": client_id,
        "redirect_url": redirect_url,
        "show_logs": show_logs,
        "save_logs": save_logs,
    }


def apply_user_inputs_to_config(user_inputs):
    config = get_config()
    config["email_suffix"] = user_inputs["email_suffix"]
    config["proxy_source"] = user_inputs["proxy_source"]
    config["proxy_file"] = user_inputs["proxy_file"]
    config["proxy_api_url"] = user_inputs["proxy_api_url"]
    if user_inputs.get("proxy_source") == "residential":
        config["proxy_residential_host"] = user_inputs["residential_host"]
        config["proxy_residential_port"] = user_inputs["residential_port"]
        config["proxy_residential_username"] = user_inputs["residential_username"]
        config["proxy_residential_password"] = user_inputs["residential_password"]
    config["concurrent_flows"] = user_inputs["concurrent_flows"]
    config["max_tasks"] = user_inputs["max_tasks"]
    config["save_logs"] = bool(user_inputs.get("save_logs", False))

    oauth2_config = dict(config.get("oauth2", {}))
    oauth2_config["enable_oauth2"] = user_inputs["enable_oauth2"]
    oauth2_config["client_id"] = user_inputs["client_id"]
    oauth2_config["redirect_url"] = user_inputs["redirect_url"]
    config["oauth2"] = oauth2_config

    update_config(config)
    save_config()


# ── task runner ────────────────────────────────────────────────────────


def next_flow_id():
    return f"flow-{os.getpid()}-{next(_flow_counter):06d}"


def _call_get_thread_page(controller, assigned_proxy, flow_id):
    method = controller.get_thread_page
    try:
        parameters = inspect.signature(method).parameters.values()
        supports_flow_id = any(
            parameter.name == "flow_id"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_flow_id = False

    if supports_flow_id:
        return method(assigned_proxy, flow_id=flow_id)
    return method(assigned_proxy)


def _registration_metadata(controller, result):
    typed_result = None
    accessor = getattr(controller, "get_last_registration_result", None)
    if accessor:
        try:
            typed_result = accessor()
        except Exception:
            typed_result = None
    if typed_result is None and hasattr(result, "outcome"):
        typed_result = result

    success = bool(result)
    if typed_result is None:
        return success, "success" if success else "application_error", None, ""

    outcome = str(
        getattr(typed_result, "outcome", None)
        or ("success" if success else "application_error")
    )
    failure_class = getattr(typed_result, "failure_class", None)
    detail = str(getattr(typed_result, "detail", "") or "")
    return success, outcome, failure_class, detail


def _page_proxy_metadata(controller, page):
    proxy_config = None
    accessor = getattr(controller, "get_page_proxy_config", None)
    if accessor and page is not None:
        try:
            proxy_config = accessor(page)
        except Exception:
            proxy_config = None
    return proxy_config, masked_proxy_id(proxy_config)


def _report_application_outcome(
    controller,
    page,
    outcome,
    detail,
    flow_id,
):
    reporter = getattr(controller, "report_application_outcome", None)
    if not reporter or page is None:
        return
    try:
        reporter(page, outcome, detail=detail, flow_id=flow_id)
    except TypeError:
        reporter(page, outcome)


def _emit_flow_metric(
    flow_id,
    proxy_id,
    outcome,
    failure_class,
    elapsed_ms,
):
    payload = {
        "elapsed_ms": round(elapsed_ms, 2),
        "failure_class": failure_class,
        "flow_id": flow_id,
        "outcome": outcome,
        "proxy_id": proxy_id,
    }
    print("[Metric: Flow] " + json.dumps(payload, sort_keys=True))


def effective_worker_count(requested, proxy_capacity, gateway_capacity=0):
    requested = max(1, int(requested))
    proxy_capacity = int(proxy_capacity or 0)
    gateway_capacity = int(gateway_capacity or 0)
    if proxy_capacity > 0:
        requested = min(requested, proxy_capacity)
    if gateway_capacity > 0:
        requested = min(requested, gateway_capacity)
    return requested


def process_single_flow(controller, assigned_proxy=None, flow_id=None):
    flow_id = flow_id or next_flow_id()
    started = time.perf_counter()
    page = None
    registration_succeeded = False
    flow_outcome = "flow_error"
    failure_class = None
    proxy_id = "proxy-none"
    try:
        config = get_config()
        page = _call_get_thread_page(controller, assigned_proxy, flow_id)
        if not page:
            flow_outcome = "proxy_unavailable"
            failure_class = "proxy_acquire_failed"
            return {"success": False, "oauth_success": False}

        _page_proxy, proxy_id = _page_proxy_metadata(controller, page)
        email = random_email()
        password = generate_strong_password()

        transport_retry_limit = max(
            0,
            int(config.get("transport_retry_limit", 1) or 0),
        )
        for attempt in range(transport_retry_limit + 1):
            reset_result = getattr(controller, "reset_registration_result", None)
            if reset_result:
                reset_result()
            result = controller.outlook_register(page, email, password)
            (
                registration_succeeded,
                flow_outcome,
                failure_class,
                detail,
            ) = _registration_metadata(controller, result)
            if (
                registration_succeeded
                or flow_outcome != "transport_error"
                or attempt >= transport_retry_limit
            ):
                break

            print(
                "[Info: Flow Retry] - transient browser transport failure; "
                f"relaunching ({attempt + 1}/{transport_retry_limit})"
            )
            controller.clean_up(page, "done_browser")
            page = _call_get_thread_page(controller, assigned_proxy, flow_id)
            if not page:
                flow_outcome = "proxy_unavailable"
                failure_class = "proxy_acquire_failed"
                detail = "browser relaunch failed after transport error"
                break
            _page_proxy, proxy_id = _page_proxy_metadata(controller, page)
        _report_application_outcome(
            controller,
            page,
            flow_outcome,
            failure_class or detail,
            flow_id,
        )
        if registration_succeeded and not controller.enable_oauth2:
            return {"success": True, "oauth_success": False}
        if not registration_succeeded:
            return {"success": False, "oauth_success": False}

        client_id = config["oauth2"]["client_id"]
        page_proxy, _proxy_id = _page_proxy_metadata(controller, page)
        token_result = get_access_token(
            page,
            email,
            proxy_config=page_proxy,
            password=password,
        )
        if token_result[0]:
            refresh_token, _access_token, _expire_at = token_result
            full_email = f"{email}{controller.email_suffix}"
            with _token_write_lock:
                with open(
                    os.path.join(
                        PROJECT_ROOT, "Results", "outlook_token.txt"
                    ),
                    "a",
                    encoding="utf-8",
                ) as f2:
                    f2.write(
                        f"{full_email}----{password}----"
                        f"{client_id}----{refresh_token}\n"
                    )
                    f2.flush()
                    os.fsync(f2.fileno())

            promote_account = getattr(controller, "promote_registered_account", None)
            if promote_account:
                try:
                    promote_account(full_email, password)
                except Exception as persistence_error:
                    print(
                        "[Warn: Results] - OAuth account promotion failed: "
                        f"{persistence_error}"
                    )

            print(f"[Success: TokenAuth] - {full_email}")
            return {"success": True, "oauth_success": True}
        else:
            return {"success": True, "oauth_success": False}
    except Exception as e:
        if not registration_succeeded:
            flow_outcome = "flow_exception"
            failure_class = type(e).__name__
            _report_application_outcome(
                controller,
                page,
                flow_outcome,
                failure_class,
                flow_id,
            )
        print(f"[Error: Flow] - {e}")
        return {
            "success": registration_succeeded,
            "oauth_success": False,
        }
    finally:
        try:
            controller.clean_up(page, "done_browser")
        except Exception as cleanup_error:
            print(f"[Warn: Cleanup] - {cleanup_error}")
        _emit_flow_metric(
            flow_id,
            proxy_id,
            flow_outcome,
            failure_class,
            (time.perf_counter() - started) * 1000,
        )


def run_concurrent_flows(controller, concurrent_flows=10, max_tasks=100):
    task_counter = 0
    source_config = get_proxy_source_config()
    managed_pool = getattr(controller, "proxy_pool", None)
    gateway_capacity = 0
    if managed_pool is not None:
        pool_snapshot = managed_pool.snapshot()
        proxy_capacity = pool_snapshot.get("total", 0)
        if managed_pool.gateway_limit > 0:
            gateway_count = len(
                {
                    record.get("gateway_key")
                    for record in pool_snapshot.get("records", [])
                    if record.get("gateway_key")
                }
            )
            gateway_capacity = gateway_count * managed_pool.gateway_limit
    else:
        proxy_capacity = get_proxy_capacity(proxy_source=source_config.get("proxy_source"))
    worker_count = effective_worker_count(
        concurrent_flows,
        proxy_capacity,
        gateway_capacity=gateway_capacity,
    )
    config = get_config()
    if (
        source_config.get("proxy_source") == "api"
        and source_config.get("proxy_api_lease_mode", True)
    ):
        # A lease endpoint can return one fresh proxy per request, so the
        # initial response size is not a concurrency limit.
        if managed_pool is None:
            worker_count = max(1, int(concurrent_flows))
    poll_interval = max(0.05, float(config.get("scheduler_poll_interval", 0.15)))
    launch_stagger = max(0.0, float(config.get("launch_stagger", 0.15)))

    status_bar.total_tasks = max_tasks
    status_bar.enable_oauth2 = controller.enable_oauth2

    if managed_pool is not None:
        desired_ready = max(
            1,
            min(worker_count, gateway_capacity or worker_count, proxy_capacity or worker_count),
        )
        startup_timeout = max(
            0.0,
            float(config.get("proxy_startup_ready_timeout", 60)),
        )
        managed_pool.wait_for_ready(desired_ready, timeout=startup_timeout)
        ready_count = managed_pool.ready_count()
        print(
            f"[Info: Proxy Pool] - ready={ready_count}/{proxy_capacity}, "
            f"gateway_limit={managed_pool.gateway_limit or 'unlimited'}"
        )
        if ready_count <= 0:
            print("[Error: Proxy Pool] - background preflight found no ready routes")
            return {
                "requested_workers": max(1, int(concurrent_flows)),
                "worker_count": worker_count,
                "proxy_capacity": proxy_capacity,
                "gateway_capacity": gateway_capacity,
                "submitted": 0,
            }

    if worker_count != max(1, int(concurrent_flows)):
        print(
            f"[Info: Scheduler] - worker count clamped to {worker_count} "
            f"for {proxy_capacity} available proxies"
        )
    elif source_config.get("proxy_source") == "api" and source_config.get(
        "proxy_api_lease_mode", True
    ):
        print("[Info: Scheduler] - API lease mode enabled; workers will refill proxies on demand")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        running_futures = set()

        while True:
            done_futures = {f for f in running_futures if f.done()}
            for future in done_futures:
                try:
                    result = future.result()
                    if result["success"]:
                        status_bar.succeeded += 1
                        if controller.enable_oauth2:
                            if result["oauth_success"]:
                                status_bar.oauth_succeeded += 1
                            else:
                                status_bar.oauth_failed += 1
                    else:
                        status_bar.failed += 1
                except Exception as e:
                    status_bar.failed += 1
                    print(e)
                running_futures.remove(future)

            while (
                len(running_futures) < worker_count
                and task_counter < max_tasks
                and not stop_event.is_set()
            ):
                future = executor.submit(process_single_flow, controller, None)
                running_futures.add(future)
                task_counter += 1
                status_bar.submitted = task_counter
                if launch_stagger and len(running_futures) < worker_count:
                    time.sleep(launch_stagger)

            status_bar.active_threads = len(running_futures)
            should_submit = task_counter < max_tasks and not stop_event.is_set()
            if not should_submit and not running_futures:
                break

            if running_futures:
                wait(running_futures, timeout=poll_interval, return_when=FIRST_COMPLETED)
            else:
                time.sleep(poll_interval)

    status_bar.active_threads = 0
    return {
        "requested_workers": max(1, int(concurrent_flows)),
        "worker_count": worker_count,
        "proxy_capacity": proxy_capacity,
        "gateway_capacity": gateway_capacity,
        "submitted": task_counter,
    }


# ── entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    enable_windows_vt()

    user_inputs = collect_user_inputs()
    apply_user_inputs_to_config(user_inputs)

    config = get_config()
    if config.get("choose_browser", "ruyipage") != "ruyipage":
        raise SystemExit("当前版本仅支持 ruyipage，请将 choose_browser 设置为 ruyipage。")

    signal.signal(signal.SIGINT, signal_handler)

    os.makedirs(os.path.join(PROJECT_ROOT, "Results"), exist_ok=True)
    proxy_pool = None
    controller = None
    try:
        proxy_pool = ManagedProxyPool(config=config).start()
        controller = RuyiPageController(proxy_pool=proxy_pool)

        status_bar.show_logs = user_inputs["show_logs"]
        status_bar.configure_log_file(user_inputs["save_logs"])
        status_bar.start()

        if user_inputs["show_logs"]:
            print("\n  [Info] - 开始执行任务（按 Ctrl+C 优雅停止）\n")

        run_concurrent_flows(
            controller,
            user_inputs["concurrent_flows"],
            user_inputs["max_tasks"],
        )
    finally:
        try:
            if controller is not None:
                controller.clean_up(type="all_browser")
            if proxy_pool is not None:
                proxy_pool.stop()
            if stop_event.is_set():
                print("[Info] - 所有存活线程已完成，程序停止。")
            elapsed = status_bar.elapsed_seconds()
            throughput = (
                status_bar.submitted / elapsed * 60 if elapsed > 0 else 0.0
            )
            print(
                f"[Result] - 总计提交 {status_bar.submitted}, "
                f"成功 {status_bar.succeeded}, 失败 {status_bar.failed}, "
                f"耗时 {elapsed:.1f}s, 吞吐 {throughput:.1f}/分钟"
            )
            if controller is not None and controller.enable_oauth2:
                print(
                    f"[Result: TokenAuth] - OAuth2 授权成功 "
                    f"{status_bar.oauth_succeeded}, 授权失败 {status_bar.oauth_failed}"
                )
        finally:
            saved_log_path = status_bar.log_path
            status_bar.stop()
        if saved_log_path:
            print(f"[Info: Log] - {saved_log_path}")
