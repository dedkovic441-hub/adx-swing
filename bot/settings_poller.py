#!/usr/bin/env python3
"""Poll dashboard trade settings and apply them to the paper bot config.

The static GitHub Pages dashboard writes control/settings.json (via the
GitHub Contents API) or opens a 'settings:' issue in
dedkovic441-hub/zigzag-swing-dashboard. This process, started by the
bot supervisor, fetches those with the box's `gh` login, validates them,
updates bot/config.yaml (gitignored), and restarts signal_bot.py through
the existing supervisor only when values actually change.

Paper only — never touches Binance keys or places real orders.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import paper_engine as pe  # noqa: E402

log = logging.getLogger("settings_poller")

REPO = "dedkovic441-hub/zigzag-swing-dashboard"
SETTINGS_PATH = "control/settings.json"
OWNER = "dedkovic441-hub"
CONFIG_PATH = ROOT / "bot" / "config.yaml"
PID_FILE = ROOT / "run" / "settings_poller.pid"
APPLIED_PATH = ROOT / "run" / "settings_applied.json"
BOT_PID_FILE = ROOT / "run" / "bot.pid"
POLL_SEC = 20
SCAN_TIMEFRAMES = ["1m", "5m", "15m"]
ALLOWED_TFS = set(SCAN_TIMEFRAMES)
WIB = timezone(timedelta(hours=7))

# Validation bounds from the dashboard brief.
TF_OK = ALLOWED_TFS
TP_MIN, TP_MAX = 0.0, 100.0
CD_MIN, CD_MAX = 0.0, 240.0
LEV_MIN, LEV_MAX = 1.0, 20.0
SIZE_PCT_MIN, SIZE_PCT_MAX = 0.5, 50.0  # percent
DCA_EVERY_MIN, DCA_EVERY_MAX = 1, 20
MAX_ADDS_MIN, MAX_ADDS_MAX = 0, 10


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_wib(dt: datetime) -> str:
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S Asia/Jakarta")


def _fmt_num(n: float | int) -> str:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return str(n)
    if x == int(x) and abs(x) < 1e12:
        return str(int(x))
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _parse_dt(val) -> Optional[datetime]:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(val).strip()
    if not s:
        return None
    # "2026-11-18 15:23:05 Asia/Jakarta"
    s = s.replace(" Asia/Jakarta", "").replace(" WIB", "")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        # Naive stamps from the dashboard file are UTC if they look ISO with T,
        # otherwise treat as WIB when they came from updated_wib-style strings.
        if "T" in str(val):
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=WIB)
    return dt.astimezone(timezone.utc)


class SettingsError(ValueError):
    pass


def _as_float(name: str, raw, lo: float, hi: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError) as e:
        raise SettingsError(f"{name} must be a number") from e
    if val != val or val in (float("inf"), float("-inf")):
        raise SettingsError(f"{name} must be a finite number")
    if val < lo or val > hi:
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    return val


def _as_int(name: str, raw, lo: int, hi: int) -> int:
    val = _as_float(name, raw, lo, hi)
    if abs(val - round(val)) > 1e-9:
        raise SettingsError(f"{name} must be an integer")
    return int(round(val))


def _size_percent(raw: dict) -> float:
    """Accept size_percent, or size_pct as either a percent (0.5-50) or a fraction (<=1)."""
    if raw.get("size_percent") is not None:
        return _as_float("size_percent", raw.get("size_percent"), SIZE_PCT_MIN, SIZE_PCT_MAX)
    if raw.get("size_pct") is None:
        raise SettingsError("size_percent is required")
    val = raw.get("size_pct")
    try:
        n = float(val)
    except (TypeError, ValueError) as e:
        raise SettingsError("size_pct must be a number") from e
    if 0 < n <= 1.0:
        n = n * 100.0
    return _as_float("size_percent", n, SIZE_PCT_MIN, SIZE_PCT_MAX)


def validate_settings(raw: Any) -> dict:
    """Return a canonical settings dict or raise SettingsError."""
    if not isinstance(raw, dict):
        raise SettingsError("settings must be a JSON object")
    tf = str(raw.get("active_timeframe") or raw.get("timeframe") or "").strip().lower()
    if tf not in TF_OK:
        raise SettingsError("active_timeframe must be one of 1m, 5m, 15m")
    tp = _as_float("take_profit_usd", raw.get("take_profit_usd", raw.get("tp")), TP_MIN, TP_MAX)
    cd = _as_float(
        "cooldown_minutes",
        raw.get("cooldown_minutes", raw.get("cooldown")),
        CD_MIN, CD_MAX,
    )
    lev = _as_float("leverage", raw.get("leverage"), LEV_MIN, LEV_MAX)
    size_percent = _size_percent(raw)
    dca = _as_int(
        "dca_signal_every",
        raw.get("dca_signal_every", raw.get("dca_every")),
        DCA_EVERY_MIN, DCA_EVERY_MAX,
    )
    max_adds = _as_int(
        "max_add_positions",
        raw.get("max_add_positions", raw.get("max_adds")),
        MAX_ADDS_MIN, MAX_ADDS_MAX,
    )
    return {
        "active_timeframe": tf,
        "take_profit_usd": tp,
        "cooldown_minutes": cd,
        "leverage": lev,
        "size_percent": size_percent,
        "dca_signal_every": dca,
        "max_add_positions": max_adds,
    }


def settings_equal(a: dict, b: dict) -> bool:
    if not a or not b:
        return False
    try:
        va, vb = validate_settings(a), validate_settings(b)
    except SettingsError:
        return False
    return (
        va["active_timeframe"] == vb["active_timeframe"]
        and round(va["take_profit_usd"], 6) == round(vb["take_profit_usd"], 6)
        and round(va["cooldown_minutes"], 6) == round(vb["cooldown_minutes"], 6)
        and round(va["leverage"], 6) == round(vb["leverage"], 6)
        and round(va["size_percent"], 6) == round(vb["size_percent"], 6)
        and va["dca_signal_every"] == vb["dca_signal_every"]
        and va["max_add_positions"] == vb["max_add_positions"]
    )


def current_settings_from_config(path: Path = CONFIG_PATH) -> dict:
    return {
        "active_timeframe": pe.load_active_timeframe(path),
        "take_profit_usd": pe.load_take_profit_usd(path),
        "cooldown_minutes": pe.load_cooldown_minutes(path),
        "leverage": pe.load_leverage(path),
        "size_percent": pe.size_pct_to_percent(pe.load_size_pct(path)),
        "dca_signal_every": pe.load_unconfirmed_dca_signal_every(path),
        "max_add_positions": pe.load_unconfirmed_max_add(path),
    }


def _set_scalar_in_section(text: str, section: str, key: str, value_str: str) -> str:
    """Replace or insert `key: value` under a top-level YAML section."""
    lines = text.splitlines(keepends=True)
    section_at = None
    for i, line in enumerate(lines):
        raw = line.split("#", 1)[0]
        if raw.strip().rstrip(":") == section and raw.startswith(section):
            section_at = i
            break
    if section_at is None:
        nl = "" if text.endswith("\n") or not text else "\n"
        return text + f"{nl}{section}:\n  {key}: {value_str}\n"
    # Find the range of the section (until next top-level key).
    end = len(lines)
    for j in range(section_at + 1, len(lines)):
        raw = lines[j].split("#", 1)[0]
        if raw.strip() and not raw.startswith(" ") and not raw.startswith("\t"):
            end = j
            break
    key_at = None
    for j in range(section_at + 1, end):
        raw = lines[j].split("#", 1)[0]
        if raw.strip().startswith(f"{key}:"):
            key_at = j
            break
    new_line = f"  {key}: {value_str}\n"
    if key_at is not None:
        # Keep trailing comment if the line had one after the value.
        old = lines[key_at]
        comment = ""
        if "#" in old:
            # only keep a comment that is not inside a string we don't control
            after = old.split("#", 1)[1]
            comment = "  #" + after.rstrip("\n")
            if not comment.endswith("\n"):
                comment = comment  # value line already has newline via new_line
            lines[key_at] = f"  {key}: {value_str}  #{after}" if after.strip() else new_line
            if not lines[key_at].endswith("\n"):
                lines[key_at] += "\n"
        else:
            # preserve original newline style
            ending = "\n" if old.endswith("\n") else ""
            lines[key_at] = f"  {key}: {value_str}{ending or chr(10)}"
    else:
        # Insert after the last existing key in the section, before a blank at end.
        insert_at = end
        for j in range(end - 1, section_at, -1):
            if lines[j].strip():
                insert_at = j + 1
                break
        lines.insert(insert_at, new_line)
    return "".join(lines)


def _set_top_level_list(text: str, key: str, values: list[str]) -> str:
    """Replace or insert a simple block list at the top level (e.g. timeframes)."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        raw = line.split("#", 1)[0]
        if raw.strip().startswith(f"{key}:"):
            start = i
            break
    block = [f"{key}:\n"] + [f"  - {v}\n" for v in values]
    if start is None:
        nl = "" if text.endswith("\n") or not text else "\n"
        return text + nl + "".join(block)
    # Consume following dashed items (and skip a same-line [inline] list).
    end = start + 1
    first = lines[start].split("#", 1)[0].strip()
    rest = first.split(":", 1)[1].strip()
    if rest.startswith("["):
        end = start + 1
    else:
        while end < len(lines):
            raw = lines[end].split("#", 1)[0]
            if raw.strip().startswith("-"):
                end += 1
                continue
            break
    lines[start:end] = block
    return "".join(lines)


def _set_section_list(text: str, section: str, key: str, values: list[str]) -> str:
    """Replace or insert a block list under a top-level section."""
    lines = text.splitlines(keepends=True)
    section_at = None
    for i, line in enumerate(lines):
        raw = line.split("#", 1)[0]
        if raw.strip().rstrip(":") == section and raw.startswith(section):
            section_at = i
            break
    block = [f"  {key}:\n"] + [f"    - {v}\n" for v in values]
    if section_at is None:
        nl = "" if text.endswith("\n") or not text else "\n"
        return text + f"{nl}{section}:\n" + "".join(block)
    end = len(lines)
    for j in range(section_at + 1, len(lines)):
        raw = lines[j].split("#", 1)[0]
        if raw.strip() and not raw.startswith(" ") and not raw.startswith("\t"):
            end = j
            break
    key_at = None
    for j in range(section_at + 1, end):
        raw = lines[j].split("#", 1)[0]
        if raw.strip().startswith(f"{key}:"):
            key_at = j
            break
    if key_at is None:
        insert_at = end
        for j in range(end - 1, section_at, -1):
            if lines[j].strip():
                insert_at = j + 1
                break
        lines[insert_at:insert_at] = block
        return "".join(lines)
    # Replace the key line and following dashed items.
    item_end = key_at + 1
    rest = lines[key_at].split("#", 1)[0].strip().split(":", 1)[1].strip()
    if not rest.startswith("["):
        while item_end < end:
            raw = lines[item_end].split("#", 1)[0]
            if raw.strip().startswith("-"):
                item_end += 1
                continue
            break
    lines[key_at:item_end] = block
    return "".join(lines)


def apply_settings_to_config(path: Path, settings: dict) -> bool:
    """Write validated settings into config.yaml. Returns True if the file changed.

    Also keeps scanner timeframes and unconfirmed.dca_every_signal_timeframes
    as 1m, 5m, 15m so a 1m active timeframe has signals to trade.
    """
    s = validate_settings(settings)
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    original = path.read_text(encoding="utf-8")
    text = original
    text = _set_top_level_list(text, "timeframes", SCAN_TIMEFRAMES)
    text = _set_scalar_in_section(text, "paper", "active_timeframe", s["active_timeframe"])
    text = _set_scalar_in_section(text, "paper", "take_profit_usd", _fmt_num(s["take_profit_usd"]))
    text = _set_scalar_in_section(text, "paper", "leverage", _fmt_num(s["leverage"]))
    text = _set_scalar_in_section(text, "paper", "cooldown_minutes", _fmt_num(s["cooldown_minutes"]))
    text = _set_scalar_in_section(text, "paper", "size_pct", _fmt_num(s["size_percent"] / 100.0))
    text = _set_scalar_in_section(text, "unconfirmed", "max_add_positions", _fmt_num(s["max_add_positions"]))
    text = _set_scalar_in_section(text, "unconfirmed", "dca_signal_every", _fmt_num(s["dca_signal_every"]))
    text = _set_section_list(text, "unconfirmed", "dca_every_signal_timeframes", SCAN_TIMEFRAMES)
    if text != original:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return True
    return False


def parse_issue_body(body: str) -> dict:
    """Extract a JSON settings object from an issue body."""
    text = body or ""
    # Fenced json block first, then any fenced block, then a bare object.
    patterns = [
        r"```json\s*(\{.*?\})\s*```",
        r"```\s*(\{.*?\})\s*```",
        r"(\{[^{}]*active_timeframe[^{}]*\})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.S)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        return validate_settings(obj)
    raise SettingsError("issue body has no valid settings JSON")


def write_applied(settings: dict, *, source: str, restarted: bool) -> None:
    now = now_utc()
    payload = dict(validate_settings(settings))
    payload["applied_utc"] = fmt_utc(now)
    payload["applied_wib"] = fmt_wib(now)
    payload["source"] = source
    payload["restarted"] = bool(restarted)
    APPLIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPLIED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(APPLIED_PATH)


def load_applied() -> Optional[dict]:
    try:
        data = json.loads(APPLIED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def restart_bot() -> bool:
    """Ask the supervisor to respawn signal_bot by killing the current pid.

    Returns True if a pid was signaled. The supervisor loop starts a new bot.
    """
    try:
        raw = BOT_PID_FILE.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except Exception:
        log.warning("no bot pid file; supervisor should start one")
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        log.info("signaled bot pid %s to restart", pid)
        return True
    except ProcessLookupError:
        log.warning("bot pid %s already gone", pid)
        return False


def _gh(args: list[str], timeout: int = 40) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def fetch_settings_file() -> Optional[dict]:
    """Return {settings, sha, updated} from control/settings.json on main, or None."""
    proc = _gh([
        "api",
        "-H", "Accept: application/vnd.github+json",
        f"repos/{REPO}/contents/{SETTINGS_PATH}?ref=main",
    ])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:400]
        if "404" in err or "Not Found" in err:
            return None
        raise RuntimeError(f"gh contents: {err}")
    data = json.loads(proc.stdout)
    raw = data.get("content") or ""
    decoded = base64.b64decode(raw.replace("\n", "")).decode("utf-8")
    obj = json.loads(decoded)
    settings = validate_settings(obj)
    updated = _parse_dt(obj.get("updated_utc") or obj.get("updated_wib")) or now_utc()
    return {
        "settings": settings,
        "sha": data.get("sha"),
        "updated": updated,
        "source": "file",
        "raw": obj,
    }


def fetch_settings_issues() -> list[dict]:
    proc = _gh([
        "issue", "list",
        "--repo", REPO,
        "--state", "open",
        "--author", OWNER,
        "--limit", "30",
        "--json", "number,title,body,createdAt,updatedAt,author",
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue list: {(proc.stderr or proc.stdout or '')[:400]}")
    rows = json.loads(proc.stdout or "[]")
    out = []
    for row in rows:
        title = str(row.get("title") or "")
        if not title.lower().startswith("settings:"):
            continue
        author = ((row.get("author") or {}).get("login") or "")
        if author and author != OWNER:
            continue
        try:
            settings = parse_issue_body(row.get("body") or "")
        except SettingsError as e:
            log.warning("skip issue #%s: %s", row.get("number"), e)
            continue
        updated = _parse_dt(row.get("updatedAt") or row.get("createdAt")) or now_utc()
        out.append({
            "settings": settings,
            "updated": updated,
            "source": f"issue #{row.get('number')}",
            "number": row.get("number"),
        })
    return out


def comment_and_close_issue(number: int, body: str) -> None:
    c = _gh(["issue", "comment", str(number), "--repo", REPO, "--body", body])
    if c.returncode != 0:
        log.warning("issue comment #%s failed: %s", number, (c.stderr or "")[:240])
    x = _gh(["issue", "close", str(number), "--repo", REPO, "--reason", "completed"])
    if x.returncode != 0:
        log.warning("issue close #%s failed: %s", number, (x.stderr or "")[:240])


def maybe_write_settings_file(settings: dict, sha: Optional[str]) -> None:
    """Keep control/settings.json in sync when we applied an issue (best-effort)."""
    now = now_utc()
    payload = dict(validate_settings(settings))
    payload["updated_utc"] = fmt_utc(now)
    payload["updated_wib"] = fmt_wib(now)
    content = base64.b64encode(
        (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    ).decode("ascii")
    body: dict[str, Any] = {
        "message": "Update paper trade settings (applied from issue)",
        "content": content,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    proc = subprocess.run(
        [
            "gh", "api", "--method", "PUT",
            "-H", "Accept: application/vnd.github+json",
            f"repos/{REPO}/contents/{SETTINGS_PATH}",
            "--input", "-",
        ],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=40,
    )
    if proc.returncode != 0:
        log.warning("could not write settings.json: %s", (proc.stderr or proc.stdout or "")[:300])
    else:
        log.info("updated %s on GitHub", SETTINGS_PATH)


def pick_candidate(file_hit: Optional[dict], issues: list[dict]) -> Optional[dict]:
    cands = [c for c in ([file_hit] if file_hit else []) + list(issues) if c]
    if not cands:
        return None
    cands.sort(key=lambda c: c["updated"], reverse=True)
    return cands[0]


def poll_once() -> str:
    """Fetch, validate, apply if needed. Returns a short status token."""
    current = current_settings_from_config()
    try:
        file_hit = fetch_settings_file()
    except SettingsError as e:
        log.warning("settings.json invalid: %s", e)
        file_hit = None
    except Exception as e:
        log.warning("settings.json fetch failed: %s", e)
        file_hit = None
    try:
        issues = fetch_settings_issues()
    except Exception as e:
        log.warning("issues fetch failed: %s", e)
        issues = []

    cand = pick_candidate(file_hit, issues)
    if not cand:
        return "idle"

    incoming = cand["settings"]
    source = cand["source"]
    if settings_equal(incoming, current):
        # Close a leftover issue that already matches live config.
        if cand.get("number"):
            comment_and_close_issue(
                int(cand["number"]),
                f"No change — these settings were already live at {fmt_wib(now_utc())}.",
            )
            log.info("closed %s (already live)", source)
        applied = load_applied()
        if not applied or not settings_equal(applied, current):
            write_applied(current, source=source, restarted=False)
        return "unchanged"

    log.info("applying %s: %s", source, incoming)
    apply_settings_to_config(CONFIG_PATH, incoming)
    # Confirm what was written.
    after = current_settings_from_config()
    if not settings_equal(after, incoming):
        log.error("config did not match after apply: %s vs %s", after, incoming)
        return "apply_failed"
    restarted = restart_bot()
    write_applied(after, source=source, restarted=restarted)
    if cand.get("number"):
        comment_and_close_issue(
            int(cand["number"]),
            "Applied at "
            + fmt_wib(now_utc())
            + ". Paper bot is restarting via the supervisor. "
            + "Effective settings: "
            + json.dumps(after),
        )
        try:
            sha = (file_hit or {}).get("sha")
            maybe_write_settings_file(after, sha)
        except Exception as e:
            log.warning("sync settings.json failed: %s", e)
    log.info("applied %s restarted=%s", source, restarted)
    return "applied"


def _write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def _clear_pid() -> None:
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        if raw == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


_stop = False


def _handle_stop(signum, frame) -> None:  # noqa: ARG001
    global _stop
    _stop = True
    log.info("stopping on signal %s", signum)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s settings-poller %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Log timestamps are local; the box TZ is Asia/Jakarta.
    _write_pid()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    log.info("watching %s/%s and owner issues titled 'settings:'", REPO, SETTINGS_PATH)
    # Write applied snapshot for the publisher if missing, without restarting.
    try:
        current = current_settings_from_config()
        applied = load_applied()
        if not applied or not settings_equal(applied, current):
            write_applied(current, source="startup", restarted=False)
    except Exception as e:
        log.warning("startup snapshot failed: %s", e)
    while not _stop:
        try:
            poll_once()
        except Exception:
            log.exception("poll failed")
        for _ in range(POLL_SEC):
            if _stop:
                break
            time.sleep(1)
    _clear_pid()
    return 0


if __name__ == "__main__":
    sys.exit(main())
