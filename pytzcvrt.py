#!/usr/bin/env python3
"""
Dynamic time zone TUI (curses + zoneinfo).

- ASCII-only UI
- Configurable list of selected time zones
- Settings modal with filter, add/remove, reorder, and sorting
- Country-grouped browsing mode using tzdata mapping files
"""

from __future__ import annotations

import curses
import json
import locale
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".pytzcvrt.json")
DEFAULT_SELECTED = [
    "Asia/Baghdad",
    "Europe/Stockholm",
    "America/Los_Angeles",
    "UTC",
]

TZDATA_ZONE1970 = "/usr/share/zoneinfo/zone1970.tab"
TZDATA_ZONE = "/usr/share/zoneinfo/zone.tab"
TZDATA_ISO = "/usr/share/zoneinfo/iso3166.tab"

INPUT_FMT = "%Y-%m-%d %H:%M"
INPUT_DISPLAY = "YYYY-MM-DD HH:MM"
INPUT_LEN = len(INPUT_DISPLAY)
INPUT_TEMPLATE = "0000-00-00 00:00"
DIGIT_POSITIONS = [i for i, ch in enumerate(INPUT_TEMPLATE) if ch.isdigit()]
DIGIT_SET = set(DIGIT_POSITIONS)
FIRST_DIGIT = DIGIT_POSITIONS[0]
LAST_DIGIT = DIGIT_POSITIONS[-1]
TIME_FIRST_DIGIT = INPUT_TEMPLATE.index(" ") + 1

SORT_MODES = [
    "Name (A-Z)",
    "UTC offset now",
    "UTC offset at span start",
]

VIEW_MODES = ["flat", "country"]

# Box drawing styles (ASCII default, Unicode optional).
# Settings toggles this and we apply on Save via apply_box_mode().
BOX_STYLES = {
    "ascii": {
        "tl": "+",
        "tr": "+",
        "bl": "+",
        "br": "+",
        "h": "-",
        "v": "|",
        "tee_l": "+",
        "tee_r": "+",
        "tee_u": "+",
        "tee_d": "+",
        "cross": "+",
    },
    "unicode": {
        "tl": "┌",
        "tr": "┐",
        "bl": "└",
        "br": "┘",
        "h": "─",
        "v": "│",
        "tee_l": "├",
        "tee_r": "┤",
        "tee_u": "┬",
        "tee_d": "┴",
        "cross": "┼",
    },
}

# Mouse support (REPORT_MOUSE_POSITION may be missing on some terminals)
MOUSE_REPORT_POS = getattr(curses, "REPORT_MOUSE_POSITION", 0)
MOUSE_MASK = curses.ALL_MOUSE_EVENTS | MOUSE_REPORT_POS

CP_HEADER = 1
CP_BORDER = 2
CP_BUTTON = 3


@dataclass
class Row:
    kind: str  # 'header' or 'tz'
    label: str
    tz_name: str | None = None
    country_name: str | None = None
    country_code: str | None = None


# -----------------------------
# Helpers: formatting and digits
# -----------------------------

def format_offset(offset: timedelta | None, with_colon: bool = True) -> str:
    if offset is None:
        return "+00:00" if with_colon else "+0000"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if with_colon:
        return f"{sign}{hours:02d}:{minutes:02d}"
    return f"{sign}{hours:02d}{minutes:02d}"


def format_dt_full(dt: datetime) -> str:
    base = dt.strftime("%Y-%m-%d %H:%M:%S")
    tzname = dt.tzname() or "UTC"
    off = format_offset(dt.utcoffset(), with_colon=True)
    return f"{base} {tzname} ({off})"


def format_dt_compact(dt: datetime) -> str:
    base = dt.strftime("%Y-%m-%d %H:%M")
    tzname = dt.tzname() or "UTC"
    off = format_offset(dt.utcoffset(), with_colon=False)
    return f"{base} {tzname}{off}"


def format_dt_local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def format_dt_local_seconds(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(text: str) -> datetime:
    return datetime.strptime(text.strip(), INPUT_FMT)


def next_digit_pos(pos: int) -> int:
    for d in DIGIT_POSITIONS:
        if d >= pos:
            return d
    return LAST_DIGIT


def prev_digit_before(pos: int) -> int:
    for d in reversed(DIGIT_POSITIONS):
        if d < pos:
            return d
    return pos


def next_digit_after(pos: int) -> int:
    for d in DIGIT_POSITIONS:
        if d > pos:
            return d
    return pos


def cursor_from_click(rel: int) -> int:
    if rel <= FIRST_DIGIT:
        return FIRST_DIGIT
    if rel >= LAST_DIGIT:
        return LAST_DIGIT
    if rel in DIGIT_SET:
        return rel
    for d in DIGIT_POSITIONS:
        if d > rel:
            return d
    return LAST_DIGIT


# -----------------------------
# Config
# -----------------------------

def load_config(all_zones: set[str]) -> tuple[list[str], str]:
    selected: list[str] = []
    box_mode = "ascii"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                if isinstance(data.get("selected"), list):
                    for item in data["selected"]:
                        if isinstance(item, str) and item in all_zones:
                            selected.append(item)
                mode = data.get("box_drawing")
                if isinstance(mode, str) and mode.lower() in ("ascii", "unicode"):
                    box_mode = mode.lower()
    except FileNotFoundError:
        pass
    except Exception:
        pass

    if not selected:
        for item in DEFAULT_SELECTED:
            if item in all_zones:
                selected.append(item)

    if not selected and "UTC" in all_zones:
        selected.append("UTC")

    return selected, box_mode


def save_config(selected: list[str], box_mode: str) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"selected": selected, "box_drawing": box_mode}, f, indent=2)
    except Exception:
        pass


# -----------------------------
# tzdata country mapping
# -----------------------------

def load_country_timezones(all_zones: set[str]) -> tuple[dict[tuple[str, str], list[str]], str | None]:
    # Parse iso3166.tab
    iso_map: dict[str, str] = {}
    try:
        with open(TZDATA_ISO, "r", encoding="utf-8") as f:
            for line in f:
                if not line or line.startswith("#"):
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    iso_map[parts[0]] = parts[1]
    except Exception as exc:
        return {}, f"Failed to read {TZDATA_ISO}: {exc}"

    zone_file = None
    if os.path.exists(TZDATA_ZONE1970):
        zone_file = TZDATA_ZONE1970
    elif os.path.exists(TZDATA_ZONE):
        zone_file = TZDATA_ZONE
    else:
        return {}, "tzdata zone file not found"

    mapping: dict[tuple[str, str], set[str]] = {}
    try:
        with open(zone_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line or line.startswith("#"):
                    continue
                parts = line.strip().split("\t")
                if len(parts) < 3:
                    continue
                cc_list = parts[0].split(",")
                tz_name = parts[2]
                if tz_name not in all_zones:
                    continue
                for cc in cc_list:
                    name = iso_map.get(cc, cc)
                    key = (name, cc)
                    mapping.setdefault(key, set()).add(tz_name)
    except Exception as exc:
        return {}, f"Failed to read {zone_file}: {exc}"

    final: dict[tuple[str, str], list[str]] = {}
    for key, tzs in mapping.items():
        final[key] = sorted(tzs)

    if not final:
        return {}, "No country/timezone mappings found"

    return final, None


def build_country_rows(mapping: dict[tuple[str, str], list[str]], filter_text: str) -> list[Row]:
    rows: list[Row] = []
    filt = filter_text.strip().lower()

    for (country_name, cc) in sorted(mapping.keys(), key=lambda k: (k[0], k[1])):
        tzs = mapping[(country_name, cc)]
        country_match = False
        if filt:
            if filt in country_name.lower() or filt in cc.lower():
                country_match = True

        matched_tzs: list[str] = []
        if not filt:
            matched_tzs = list(tzs)
        else:
            for tz in tzs:
                if filt in tz.lower() or country_match:
                    matched_tzs.append(tz)

        if matched_tzs:
            header = f"{country_name} ({cc})"
            rows.append(Row("header", header, country_name=country_name, country_code=cc))
            for tz in matched_tzs:
                rows.append(Row("tz", f"  {tz}", tz_name=tz, country_name=country_name, country_code=cc))

    return rows


def next_selectable_index(rows: list[Row], start_idx: int, direction: int) -> int:
    if not rows:
        return 0
    idx = max(0, min(start_idx, len(rows) - 1))
    while 0 <= idx < len(rows):
        if rows[idx].kind == "tz":
            return idx
        idx += direction
    return max(0, min(start_idx, len(rows) - 1))


# -----------------------------
# Conversion
# -----------------------------

def compute_results(state: dict) -> None:
    state["error"] = ""
    state["results"] = []
    state["duration_str"] = ""
    state["total_minutes"] = 0

    if not state["selected"]:
        state["error"] = "No selected zones. Use settings to add at least one."
        return

    from_tz_name = state["selected"][state["from_idx"]]
    from_tz = ZoneInfo(from_tz_name)

    try:
        start_naive = parse_dt(state["start_text"])
    except ValueError:
        state["error"] = "Bad datetime format for Start. Use YYYY-MM-DD HH:MM."
        return

    try:
        end_naive = parse_dt(state["end_text"])
    except ValueError:
        state["error"] = "Bad datetime format for End. Use YYYY-MM-DD HH:MM."
        return

    start = start_naive.replace(tzinfo=from_tz)
    end = end_naive.replace(tzinfo=from_tz)

    if end < start:
        state["error"] = "End earlier than start."
        return

    duration = end - start
    total_minutes = int(duration.total_seconds() // 60)
    dur_h = total_minutes // 60
    dur_m = total_minutes % 60
    state["duration_str"] = f"{dur_h:02d}:{dur_m:02d}"
    state["total_minutes"] = total_minutes

    results = []
    for tz_name in state["selected"]:
        tz = ZoneInfo(tz_name)
        results.append((
            tz_name,
            start.astimezone(tz),
            end.astimezone(tz),
        ))

    state["results"] = results


def get_span_start_instant(state: dict) -> datetime | None:
    if not state["selected"]:
        return None
    from_tz = ZoneInfo(state["selected"][state["from_idx"]])
    try:
        start_naive = parse_dt(state["start_text"])
    except ValueError:
        return None
    return start_naive.replace(tzinfo=from_tz)


def offset_seconds_for(tz_name: str, instant: datetime | None) -> int:
    tz = ZoneInfo(tz_name)
    if instant is None:
        dt = datetime.now(tz)
    else:
        dt = instant.astimezone(tz)
    off = dt.utcoffset()
    return int(off.total_seconds()) if off else 0


# -----------------------------
# Rendering helpers
# -----------------------------

def safe_addstr(stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    # Avoid writing into the bottom-right cell; it can raise ERR on some terminals.
    if y == h - 1 and x == w - 1:
        return
    max_len = w - x
    if y == h - 1:
        max_len -= 1
    if max_len <= 0:
        return
    stdscr.addstr(y, x, text[:max_len], attr)


def add_region(
    regions: list[tuple[int, int, int, int, str, object | None]],
    y1: int,
    x1: int,
    y2: int,
    x2: int,
    action: str,
    payload: object | None = None,
) -> None:
    if x2 < x1 or y2 < y1:
        return
    regions.append((y1, x1, y2, x2, action, payload))


def region_hit(
    regions: list[tuple[int, int, int, int, str, object | None]],
    x: int,
    y: int,
) -> tuple[str, object | None] | None:
    for y1, x1, y2, x2, action, payload in reversed(regions):
        if y1 <= y <= y2 and x1 <= x <= x2:
            return action, payload
    return None


def draw_button(
    stdscr: curses.window,
    y: int,
    x: int,
    key_char: str,
    label_rest: str,
    focused: bool = False,
    enabled: bool = True,
    color_pair: int | None = None,
) -> int:
    # Underline only the key character inside brackets: "[" + key + "]" + rest
    # This remains ASCII-only; underline is an attribute, not a glyph.
    key = key_char[:1] if key_char else " "
    base_attr = 0
    if color_pair:
        base_attr |= curses.color_pair(color_pair)
    if focused:
        base_attr |= curses.A_REVERSE
    if not enabled:
        base_attr |= curses.A_DIM
    underline_attr = base_attr | curses.A_UNDERLINE

    safe_addstr(stdscr, y, x, "[", base_attr)
    safe_addstr(stdscr, y, x + 1, key, underline_attr)
    safe_addstr(stdscr, y, x + 2, "]" + label_rest, base_attr)

    return 3 + len(label_rest)


def draw_field(
    stdscr: curses.window,
    y: int,
    x: int,
    label: str,
    value: str,
    width: int,
    focused: bool,
    cursor_pos: int,
) -> tuple[int, tuple[int, int] | None]:
    label_text = f"{label}: "
    safe_addstr(stdscr, y, x, label_text)
    x += len(label_text)

    safe_addstr(stdscr, y, x, "[")
    x += 1

    display = (value + " " * width)[:width]
    safe_addstr(stdscr, y, x, display)

    cursor = None
    if focused:
        cur_idx = max(0, min(cursor_pos, width - 1))
        cursor_x = x + cur_idx
        cursor = (y, cursor_x)
        safe_addstr(stdscr, y, cursor_x, display[cur_idx], curses.A_REVERSE)

    x += width
    safe_addstr(stdscr, y, x, "]")
    x += 1

    return x, cursor


def draw_hline(stdscr: curses.window, y: int, x: int, length: int, ch: str) -> None:
    if length <= 0:
        return
    safe_addstr(stdscr, y, x, ch * length)


def draw_vline(stdscr: curses.window, y: int, x: int, length: int, ch: str) -> None:
    for i in range(max(0, length)):
        safe_addstr(stdscr, y + i, x, ch)


def draw_box(stdscr: curses.window, y: int, x: int, h: int, w: int, style: dict) -> None:
    if h < 2 or w < 2:
        return
    draw_hline(stdscr, y, x + 1, w - 2, style["h"])
    draw_hline(stdscr, y + h - 1, x + 1, w - 2, style["h"])
    draw_vline(stdscr, y + 1, x, h - 2, style["v"])
    draw_vline(stdscr, y + 1, x + w - 1, h - 2, style["v"])
    def addch_try(yy: int, xx: int, ch: str) -> None:
        hh, ww = stdscr.getmaxyx()
        if yy < 0 or yy >= hh or xx < 0 or xx >= ww:
            return
        try:
            stdscr.addstr(yy, xx, ch)
        except curses.error:
            pass
    addch_try(y, x, style["tl"])
    addch_try(y, x + w - 1, style["tr"])
    addch_try(y + h - 1, x, style["bl"])
    addch_try(y + h - 1, x + w - 1, style["br"])


def ensure_visible(idx: int, scroll: int, height: int, total: int) -> int:
    if total <= height:
        return 0
    if idx < scroll:
        return idx
    if idx >= scroll + height:
        return max(0, idx - height + 1)
    return scroll


def clamp_scroll(scroll: int, height: int, total: int) -> int:
    if total <= height:
        return 0
    return max(0, min(scroll, total - height))


# -----------------------------
# Settings list helpers
# -----------------------------

def get_all_list(state: dict) -> list[str]:
    zones = state["all_zones"]
    filt = state["filter_text"].strip().lower()
    if filt:
        zones = [z for z in zones if filt in z.lower()]

    mode = state["sort_mode"]
    if mode == 0:
        return sorted(zones)

    if mode == 1:
        return sorted(
            zones,
            key=lambda z: (offset_seconds_for(z, None), z),
        )

    # mode == 2: offset at span start
    instant = get_span_start_instant(state)
    return sorted(
        zones,
        key=lambda z: (offset_seconds_for(z, instant), z),
    )


# -----------------------------
# Mouse setup
# -----------------------------

def set_mouse_enabled(state: dict, enabled: bool) -> bool:
    if enabled:
        try:
            avail, _ = curses.mousemask(MOUSE_MASK)
        except curses.error:
            state["mouse_enabled"] = False
            return False
        try:
            curses.mouseinterval(0)
        except curses.error:
            pass
        state["mouse_enabled"] = avail != 0
        return state["mouse_enabled"]

    try:
        curses.mousemask(0)
    except curses.error:
        pass
    state["mouse_enabled"] = False
    return False


def init_colors(state: dict) -> None:
    state["colors"] = False
    if curses.has_colors():
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(CP_HEADER, curses.COLOR_CYAN, -1)
            curses.init_pair(CP_BORDER, curses.COLOR_YELLOW, -1)
            curses.init_pair(CP_BUTTON, curses.COLOR_GREEN, -1)
            state["colors"] = True
        except curses.error:
            state["colors"] = False


def env_allows_unicode() -> bool:
    enc = (sys.stdout.encoding or "").lower()
    loc = (locale.getpreferredencoding(False) or "").lower()
    return "utf-8" in enc and "utf-8" in loc


def unicode_supported(stdscr: curses.window) -> bool:
    if not env_allows_unicode():
        return False
    try:
        # Use a 2x2 window so (0,0) is not the bottom-right cell.
        # Writing to bottom-right can raise ERR on some terminals.
        win = curses.newwin(2, 2, 0, 0)
        win.addstr(0, 0, BOX_STYLES["unicode"]["tl"])
        return True
    except curses.error:
        return False
    except Exception:
        return False


def apply_box_mode(state: dict, desired: str) -> None:
    # Toggle is applied at runtime after Settings Save.
    if desired == "unicode" and not state.get("unicode_supported"):
        state["box_mode"] = "ascii"
        state["box_warning"] = "Unicode box drawing not supported; using ASCII."
    else:
        state["box_mode"] = desired
        state["box_warning"] = ""
    state["box_style"] = BOX_STYLES[state["box_mode"]]


# -----------------------------
# Main screen render
# -----------------------------

def render_main(stdscr: curses.window, state: dict) -> None:
    stdscr.erase()
    regions: list[tuple[int, int, int, int, str, object | None]] = []
    state["regions"] = regions

    h, w = stdscr.getmaxyx()
    state["last_size"] = (h, w)
    style = state.get("box_style", BOX_STYLES["ascii"])
    ox = 1 if w >= 3 else 0
    oy = 1 if h >= 3 else 0
    iw = w - (ox * 2)
    ih = h - (oy * 2)

    if iw < 80 or ih < 20:
        safe_addstr(stdscr, 0, 0, "Window too small. Need at least 80x20.")
        safe_addstr(stdscr, 1, 0, f"Current size: {w}x{h}.")
        stdscr.refresh()
        return

    if ox and oy:
        draw_box(stdscr, 0, 0, h, w, style)

    def add(y: int, x: int, text: str, attr: int = 0) -> None:
        safe_addstr(stdscr, oy + y, ox + x, text, attr)

    def btn(y: int, x: int, key_char: str, label_rest: str, action: str) -> int:
        cp = CP_BUTTON if state.get("colors") else None
        width = draw_button(stdscr, oy + y, ox + x, key_char, label_rest, False, True, cp)
        add_region(regions, oy + y, ox + x, oy + y, ox + x + width - 1, action)
        return x + width + 1

    def draw_inner_hline(y: int) -> None:
        if iw <= 1:
            return
        if ox and oy:
            # Use a full-width horizontal line (connects to outer box with h chars).
            draw_hline(stdscr, oy + y, ox, iw, style["h"])
        else:
            draw_hline(stdscr, y, 0, iw, style["h"])

    add(0, 0, "pytzcvrt - dynamic time zone span converter")
    mouse_status = "on" if state.get("mouse_enabled") else "off"
    add(1, 0, f"s=settings  r=reset  q=quit  Tab=next  m=mouse  Mouse: {mouse_status}")

    # Header: show now for current From TZ
    if state["selected"]:
        from_name = state["selected"][state["from_idx"]]
        now_dt = datetime.now(ZoneInfo(from_name))
        add(2, 0, f"Now ({from_name}): {format_dt_full(now_dt)}")
    else:
        add(2, 0, "Now: (no selected zones)")

    # Buttons
    line = 3
    x = 0
    x = btn(line, x, "R", "eset", "button_reset")
    x = btn(line, x, "S", "ettings", "button_settings")
    x = btn(line, x, "Q", "uit", "button_quit")

    # Span input
    draw_inner_hline(line + 1)
    line = line + 2
    add(line, 0, "Span input:")
    line += 1

    cursor_pos = None
    x = 0

    # From TZ field with [^] [v] and clickable value
    from_label = state["selected"][state["from_idx"]] if state["selected"] else "(none)"
    from_width = max(8, min(24, max((len(z) for z in state["selected"]), default=8)))

    from_label_text = "From TZ: "
    add(line, x, from_label_text)
    x += len(from_label_text)

    up_label = "[^]"
    add(line, x, up_label)
    add_region(regions, oy + line, ox + x, oy + line, ox + x + len(up_label) - 1, "from_prev")
    x += len(up_label) + 1

    value_text = (from_label + " " * from_width)[:from_width]
    value_attr = curses.A_REVERSE if state["focus_main"] == 0 else 0
    add(line, x, f"[{value_text}]", value_attr)
    add_region(regions, oy + line, ox + x, oy + line, ox + x + from_width + 1, "from_toggle")
    from_value_start = x + 1
    x += from_width + 3

    down_label = "[v]"
    add(line, x, down_label)
    add_region(regions, oy + line, ox + x, oy + line, ox + x + len(down_label) - 1, "from_next")
    x += len(down_label) + 2

    # Start field
    start_label_text = "Start: "
    start_field_start = x
    start_value_start = x + len(start_label_text) + 1
    start_value_end = start_value_start + INPUT_LEN - 1
    start_field_end = start_value_end + 1
    start_value_start_abs = ox + start_value_start
    start_value_end_abs = ox + start_value_end
    x, cur = draw_field(
        stdscr,
        oy + line,
        ox + x,
        "Start",
        state["start_text"],
        INPUT_LEN,
        state["focus_main"] == 1,
        state["cursor_start"],
    )
    x -= ox
    add_region(
        regions,
        oy + line,
        ox + start_field_start,
        oy + line,
        ox + start_field_end,
        "focus_field",
        ("start", start_value_start_abs, start_value_end_abs),
    )
    if cur:
        cursor_pos = cur
    x += 2

    # End field
    end_label_text = "End: "
    end_field_start = x
    end_value_start = x + len(end_label_text) + 1
    end_value_end = end_value_start + INPUT_LEN - 1
    end_field_end = end_value_end + 1
    end_value_start_abs = ox + end_value_start
    end_value_end_abs = ox + end_value_end
    x, cur = draw_field(
        stdscr,
        oy + line,
        ox + x,
        "End",
        state["end_text"],
        INPUT_LEN,
        state["focus_main"] == 2,
        state["cursor_end"],
    )
    x -= ox
    add_region(
        regions,
        oy + line,
        ox + end_field_start,
        oy + line,
        ox + end_field_end,
        "focus_field",
        ("end", end_value_start_abs, end_value_end_abs),
    )
    if cur:
        cursor_pos = cur

    # Optional dropdown for From TZ
    dropdown_info = None
    if state.get("from_list_open") and state["selected"]:
        dropdown_info = (from_value_start, line + 1)

    # Results panel
    draw_inner_hline(line + 1)
    line += 2
    add(line, 0, "Results (PgUp/PgDn or mouse wheel to scroll):")
    line += 1

    if state["error"]:
        add(line, 0, f"Error: {state['error']}")
        line += 1
    else:
        add(line, 0, f"Duration: {state['duration_str']} ({state['total_minutes']} minutes)")
        line += 1

    min_dt = 19
    min_zone = 12
    max_zone = 28
    zone_w = max(min_zone, min(max_zone, iw - (5 + (3 * min_dt))))
    dt_w = max(min_dt, (iw - zone_w - 5) // 3)
    table_w = zone_w + (dt_w * 3) + 5
    table_left = 0
    table_top = line

    remaining = ih - table_top
    max_rows = max(1, (remaining - 3) // 2)
    results = state["results"] or []
    state["results_scroll"] = clamp_scroll(state["results_scroll"], max_rows, len(results))
    start_idx = state["results_scroll"]
    end_idx = min(len(results), start_idx + max_rows)
    row_count = max(0, end_idx - start_idx)

    table_x_abs = ox + table_left
    zone_x_abs = table_x_abs + 1
    now_x_abs = zone_x_abs + zone_w + 1
    start_x_abs = now_x_abs + dt_w + 1
    end_x_abs = start_x_abs + dt_w + 1

    def draw_table_hline(y: int, left_ch: str, right_ch: str, inter_ch: str) -> None:
        line_chars = [style["h"]] * table_w
        line_chars[0] = left_ch
        line_chars[-1] = right_ch
        sep_positions = [
            table_left + 1 + zone_w,
            table_left + 2 + zone_w + dt_w,
            table_left + 3 + zone_w + (dt_w * 2),
        ]
        for sx in sep_positions:
            pos = sx - table_left
            if 0 < pos < table_w - 1:
                line_chars[pos] = inter_ch
        add(y, table_left, "".join(line_chars))

    vch = style["v"]
    header_row = (
        f"{vch}{'Zone':<{zone_w}}"
        f"{vch}{'Now':<{dt_w}}"
        f"{vch}{'Start':<{dt_w}}"
        f"{vch}{'End':<{dt_w}}{vch}"
    )

    draw_table_hline(table_top, style["tl"], style["tr"], style["tee_u"])
    add(table_top + 1, table_left, header_row)
    draw_table_hline(table_top + 2, style["tee_l"], style["tee_r"], style["cross"])

    row_start = table_top + 3
    for row_i in range(row_count):
        y = row_start + (row_i * 2)
        idx = start_idx + row_i
        tz_name, start_dt, end_dt = results[idx]
        now_dt = datetime.now(ZoneInfo(tz_name))
        tz_abbr = now_dt.tzname() or "UTC"
        tz_off = format_offset(now_dt.utcoffset(), with_colon=False)
        zone_label = f"{tz_name} {tz_abbr}{tz_off}"
        now_s = format_dt_local_seconds(now_dt)
        start_s = format_dt_local(start_dt)
        end_s = format_dt_local(end_dt)

        row_text = (
            f"{vch}{zone_label:<{zone_w}}"
            f"{vch}{now_s:<{dt_w}}"
            f"{vch}{start_s:<{dt_w}}"
            f"{vch}{end_s:<{dt_w}}{vch}"
        )
        add(y, table_left, row_text)
        if row_i < row_count - 1:
            draw_table_hline(y + 1, style["tee_l"], style["tee_r"], style["cross"])

    if row_count > 0:
        bottom_y = row_start + (row_count - 1) * 2 + 1
    else:
        bottom_y = table_top + 2
    draw_table_hline(bottom_y, style["bl"], style["br"], style["tee_d"])

    state["results_row_start"] = oy + row_start
    state["results_layout"] = {
        "row_start": oy + row_start,
        "row_step": 2,
        "start_idx": start_idx,
        "visible": row_count,
        "zone_w": zone_w,
        "dt_w": dt_w,
        "table_x": table_x_abs,
    }

    # Draw dropdown overlay after results for true overlay
    if dropdown_info:
        style = state.get("box_style", BOX_STYLES["ascii"])
        dropdown_x, dropdown_y = dropdown_info
        list_h = ih - dropdown_y - 1
        list_h = max(1, list_h)
        total = len(state["selected"])
        state["from_list_scroll"] = clamp_scroll(state["from_list_scroll"], list_h, total)
        state["from_list_scroll"] = ensure_visible(
            state["from_list_idx"],
            state["from_list_scroll"],
            list_h,
            total,
        )
        start = state["from_list_scroll"]
        end = min(total, start + list_h)
        labels = [state["selected"][i] for i in range(start, end)]
        max_len = max((len(s) for s in labels), default=1)
        item_w = max_len + 2
        box_w = item_w + 2
        box_h = (end - start) + 2
        box_x = ox + max(0, dropdown_x - 1)
        box_y = oy + dropdown_y
        draw_box(stdscr, box_y, box_x, box_h, box_w, style)
        for i in range(start, end):
            label = state["selected"][i]
            item_y = box_y + 1 + (i - start)
            item_text = f" {label.ljust(max_len)} "
            attr = curses.A_REVERSE if i == state["from_list_idx"] else 0
            safe_addstr(stdscr, item_y, box_x + 1, item_text, attr)
            add_region(
                regions,
                item_y,
                box_x + 1,
                item_y,
                box_x + 1 + len(item_text) - 1,
                "from_select",
                i,
            )

    if cursor_pos:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        stdscr.move(cursor_pos[0], cursor_pos[1])
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    stdscr.refresh()


# -----------------------------
# Settings render
# -----------------------------

def render_settings(stdscr: curses.window, state: dict) -> None:
    stdscr.erase()
    regions: list[tuple[int, int, int, int, str, object | None]] = []
    state["regions"] = regions

    h, w = stdscr.getmaxyx()
    if w < 80 or h < 20:
        safe_addstr(stdscr, 0, 0, "Window too small. Need at least 80x20.")
        safe_addstr(stdscr, 1, 0, f"Current size: {w}x{h}.")
        stdscr.refresh()
        return

    style = state.get("box_style", BOX_STYLES["ascii"])
    ox = 1 if w >= 3 else 0
    oy = 1 if h >= 3 else 0
    iw = w - (ox * 2)
    ih = h - (oy * 2)
    if ox and oy:
        draw_box(stdscr, 0, 0, h, w, style)

    def add(y: int, x: int, text: str, attr: int = 0) -> None:
        safe_addstr(stdscr, oy + y, ox + x, text, attr)

    def btn(y: int, x: int, key_char: str, label_rest: str, action: str) -> int:
        cp = CP_BUTTON if state.get("colors") else None
        width = draw_button(stdscr, oy + y, ox + x, key_char, label_rest, False, True, cp)
        add_region(regions, oy + y, ox + x, oy + y, ox + x + width - 1, action)
        return x + width + 1

    add(0, 0, "Settings (s=save, c=cancel, q=cancel, Tab=focus, o=sort, v=view, b=box)")

    # Filter line
    filter_label = "Filter: "
    add(2, 0, filter_label)
    fx = len(filter_label)
    filter_text = state["filter_text"]
    add(2, fx, f"[{filter_text}]")
    add_region(regions, oy + 2, ox + fx, oy + 2, ox + fx + len(filter_text) + 1, "settings_filter")

    # Panes
    list_y = 4
    list_h = ih - list_y - 4
    list_h = max(3, list_h)
    gap = 3
    left_w = (iw - gap) // 2
    right_w = iw - gap - left_w
    left_x = 0
    right_x = left_w + gap

    # Headings
    left_title = "All time zones"
    right_title = "Selected time zones"
    add(list_y - 1, left_x, left_title)
    add(list_y - 1, right_x, right_title)

    # Build lists based on view mode
    view_mode = state["view_mode"]
    all_list: list[str] = []
    rows: list[Row] = []
    if view_mode == 0:
        all_list = get_all_list(state)
    else:
        rows = build_country_rows(state["country_map"], state["filter_text"])

    selected_list = state["settings_selected"]

    # Clamp indices
    if view_mode == 0:
        if all_list:
            state["all_idx"] = max(0, min(state["all_idx"], len(all_list) - 1))
        else:
            state["all_idx"] = 0
    else:
        if rows:
            state["all_idx"] = max(0, min(state["all_idx"], len(rows) - 1))
            if rows[state["all_idx"]].kind != "tz":
                state["all_idx"] = next_selectable_index(rows, state["all_idx"], 1)
        else:
            state["all_idx"] = 0

    if selected_list:
        state["sel_idx"] = max(0, min(state["sel_idx"], len(selected_list) - 1))
    else:
        state["sel_idx"] = 0

    # Scroll calculations
    if view_mode == 0:
        state["all_scroll"] = ensure_visible(state["all_idx"], state["all_scroll"], list_h, len(all_list))
    else:
        state["all_scroll"] = ensure_visible(state["all_idx"], state["all_scroll"], list_h, len(rows))

    state["sel_scroll"] = ensure_visible(state["sel_idx"], state["sel_scroll"], list_h, len(selected_list))

    # Draw All list
    for i in range(list_h):
        idx = state["all_scroll"] + i
        y = list_y + i
        if view_mode == 0:
            if idx >= len(all_list):
                break
            name = all_list[idx]
            attr = 0
            if state["settings_focus"] == 1 and idx == state["all_idx"]:
                attr = curses.A_REVERSE
            add(y, left_x, name[: left_w - 1], attr)
        else:
            if idx >= len(rows):
                break
            row = rows[idx]
            attr = 0
            if row.kind == "header":
                attr |= curses.A_BOLD
                if state.get("colors"):
                    attr |= curses.color_pair(CP_HEADER)
            if state["settings_focus"] == 1 and idx == state["all_idx"] and row.kind == "tz":
                attr = curses.A_REVERSE
            add(y, left_x, row.label[: left_w - 1], attr)

    # Draw Selected list
    for i in range(list_h):
        idx = state["sel_scroll"] + i
        y = list_y + i
        if idx >= len(selected_list):
            break
        name = selected_list[idx]
        attr = 0
        if state["settings_focus"] == 2 and idx == state["sel_idx"]:
            attr = curses.A_REVERSE
        add(y, right_x, name[: right_w - 1], attr)

    # Vertical separator between panes
    if gap >= 2:
        sep_x = left_x + left_w + (gap // 2)
        draw_vline(stdscr, oy + list_y - 1, ox + sep_x, list_h + 1, style["v"])

    # Store pane boxes for mouse hit-testing
    state["settings_boxes"] = {
        "all": (oy + list_y, ox + left_x, list_h, left_w),
        "selected": (oy + list_y, ox + right_x, list_h, right_w),
        "filter": (oy + 2, ox + fx, 1, max(1, iw - fx)),
    }

    # Buttons
    btn_y = ih - 3
    x = 0
    x = btn(btn_y, x, "A", "dd", "settings_add")
    x = btn(btn_y, x, "D", "elete", "settings_remove")
    x = btn(btn_y, x, "S", "ave", "settings_save")
    x = btn(btn_y, x, "C", "ancel", "settings_cancel")
    box_label = f"ox:{state.get('settings_box_mode', 'ascii').upper()}"
    x = btn(btn_y, x, "B", box_label, "settings_box")

    # Status line
    status_parts = [f"View: {VIEW_MODES[state['view_mode']]}"]
    if state["view_mode"] == 0:
        status_parts.append(f"Sort: {SORT_MODES[state['sort_mode']]}")
    status_parts.append(f"Box: {state.get('settings_box_mode', state['box_mode']).upper()}")
    if state.get("country_error"):
        status_parts.append(f"Error: {state['country_error']}")
    box_warn = state.get("box_warning")
    if state.get("settings_box_mode") == "unicode" and not state.get("unicode_supported"):
        box_warn = "Unicode box drawing not supported; using ASCII."
    if box_warn:
        status_parts.append(box_warn)
    if state.get("settings_msg"):
        status_parts.append(state["settings_msg"])
    status = " | ".join(status_parts)
    add(ih - 1, 0, status)

    # Cursor for filter
    if state["settings_focus"] == 0:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        cursor_x = fx + 1 + state["filter_cursor"]
        stdscr.move(oy + 2, min(ox + cursor_x, w - 1))
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    stdscr.refresh()


# -----------------------------
# Settings actions
# -----------------------------

def settings_add(state: dict) -> None:
    if state["view_mode"] == 0:
        all_list = get_all_list(state)
        if not all_list:
            state["settings_msg"] = "No zones to add."
            return
        name = all_list[state["all_idx"]]
    else:
        rows = build_country_rows(state["country_map"], state["filter_text"])
        if not rows:
            state["settings_msg"] = "No zones to add."
            return
        idx = state["all_idx"]
        if rows[idx].kind != "tz":
            idx = next_selectable_index(rows, idx, 1)
            if rows[idx].kind != "tz":
                state["settings_msg"] = "Select a time zone row."
                return
            state["all_idx"] = idx
        name = rows[idx].tz_name or ""

    if not name:
        state["settings_msg"] = "Select a time zone row."
        return

    try:
        ZoneInfo(name)
    except Exception:
        state["settings_msg"] = f"Invalid zone: {name}"
        return

    if name in state["settings_selected"]:
        state["settings_msg"] = "Already selected."
        return
    state["settings_selected"].append(name)
    state["settings_msg"] = f"Added {name}."


def settings_remove(state: dict) -> None:
    if len(state["settings_selected"]) <= 1:
        state["settings_msg"] = "At least one zone must remain."
        return
    if not state["settings_selected"]:
        return
    name = state["settings_selected"].pop(state["sel_idx"])
    state["sel_idx"] = max(0, min(state["sel_idx"], len(state["settings_selected"]) - 1))
    state["settings_msg"] = f"Removed {name}."


def settings_move_up(state: dict) -> None:
    idx = state["sel_idx"]
    if idx <= 0:
        return
    items = state["settings_selected"]
    items[idx - 1], items[idx] = items[idx], items[idx - 1]
    state["sel_idx"] -= 1


def settings_move_down(state: dict) -> None:
    items = state["settings_selected"]
    idx = state["sel_idx"]
    if idx >= len(items) - 1:
        return
    items[idx + 1], items[idx] = items[idx], items[idx + 1]
    state["sel_idx"] += 1


def open_settings(state: dict) -> None:
    state["settings_open"] = True
    state["settings_selected"] = list(state["selected"])
    state["settings_original"] = list(state["selected"])
    state["settings_box_mode"] = state.get("box_mode", "ascii")
    state["settings_focus"] = 1
    state["filter_text"] = ""
    state["filter_cursor"] = 0
    state["all_idx"] = 0
    state["all_scroll"] = 0
    state["sel_idx"] = 0
    state["sel_scroll"] = 0
    state["settings_msg"] = ""


def save_settings(state: dict) -> bool:
    if not state["settings_selected"]:
        state["settings_msg"] = "Select at least one zone before saving."
        return False
    state["selected"] = list(state["settings_selected"])
    state["from_idx"] = min(state["from_idx"], len(state["selected"]) - 1)
    apply_box_mode(state, state.get("settings_box_mode", state["box_mode"]))
    save_config(state["selected"], state["box_mode"])
    state["settings_open"] = False
    state["from_list_open"] = False
    compute_results(state)
    return True


def cancel_settings(state: dict) -> None:
    state["settings_open"] = False
    state["settings_selected"] = list(state["settings_original"])
    state["settings_msg"] = ""


# -----------------------------
# Input handling
# -----------------------------

def reset_inputs(state: dict) -> None:
    if not state["selected"]:
        return
    tz = ZoneInfo(state["selected"][state["from_idx"]])
    now = datetime.now(tz).replace(second=0, microsecond=0)
    start = now
    end = now + timedelta(minutes=60)
    state["start_text"] = start.strftime(INPUT_FMT)
    state["end_text"] = end.strftime(INPUT_FMT)
    state["cursor_start"] = TIME_FIRST_DIGIT
    state["cursor_end"] = TIME_FIRST_DIGIT


def handle_main_input(key: int, state: dict) -> bool:
    if key == -1:
        return False

    if key in (ord("q"), ord("Q")):
        state["quit"] = True
        return False

    if key in (ord("s"), ord("S")):
        open_settings(state)
        return True

    if key in (ord("r"), ord("R")):
        reset_inputs(state)
        compute_results(state)
        return True

    if key in (ord("m"), ord("M")):
        set_mouse_enabled(state, not state.get("mouse_enabled"))
        return True

    if key == 9:  # Tab
        state["focus_main"] = (state["focus_main"] + 1) % 3
        state["from_list_open"] = False
        if state["focus_main"] == 1:
            state["cursor_start"] = TIME_FIRST_DIGIT
        elif state["focus_main"] == 2:
            state["cursor_end"] = TIME_FIRST_DIGIT
        return True

    if key in (curses.KEY_BTAB, 353):
        state["focus_main"] = (state["focus_main"] - 1) % 3
        state["from_list_open"] = False
        if state["focus_main"] == 1:
            state["cursor_start"] = TIME_FIRST_DIGIT
        elif state["focus_main"] == 2:
            state["cursor_end"] = TIME_FIRST_DIGIT
        return True

    # From TZ selection
    if state["selected"]:
        if state.get("from_list_open"):
            if key == curses.KEY_UP:
                state["from_list_idx"] = max(0, state["from_list_idx"] - 1)
                return True
            if key == curses.KEY_DOWN:
                state["from_list_idx"] = min(len(state["selected"]) - 1, state["from_list_idx"] + 1)
                return True
            if key in (curses.KEY_ENTER, 10, 13):
                state["from_idx"] = state["from_list_idx"]
                state["from_list_open"] = False
                state["focus_main"] = 1
                state["cursor_start"] = TIME_FIRST_DIGIT
                compute_results(state)
                return True
        else:
            if key == curses.KEY_UP:
                state["from_idx"] = (state["from_idx"] - 1) % len(state["selected"])
                compute_results(state)
                return True
            if key == curses.KEY_DOWN:
                state["from_idx"] = (state["from_idx"] + 1) % len(state["selected"])
                compute_results(state)
                return True
            if state["focus_main"] == 0 and key in (curses.KEY_ENTER, 10, 13, ord(" ")):
                state["from_list_open"] = not state["from_list_open"]
                state["from_list_idx"] = state["from_idx"]
                return True

    if key in (curses.KEY_NPAGE,):
        state["results_scroll"] += 3
        return True
    if key in (curses.KEY_PPAGE,):
        state["results_scroll"] -= 3
        return True

    # Start/End input
    if state["focus_main"] in (1, 2):
        text_key = "start_text" if state["focus_main"] == 1 else "end_text"
        cursor_key = "cursor_start" if state["focus_main"] == 1 else "cursor_end"
        text = state[text_key]
        cursor = state[cursor_key]

        if key in (curses.KEY_ENTER, 10, 13):
            if state["focus_main"] == 1:
                state["focus_main"] = 2
                state["cursor_end"] = TIME_FIRST_DIGIT
            else:
                state["focus_main"] = 0
                state["from_list_open"] = False
            return True

        if key in (curses.KEY_LEFT,):
            state[cursor_key] = prev_digit_before(cursor)
            return True
        if key in (curses.KEY_RIGHT,):
            state[cursor_key] = next_digit_after(cursor)
            return True
        if key in (curses.KEY_HOME,):
            state[cursor_key] = FIRST_DIGIT
            return True
        if key in (curses.KEY_END,):
            state[cursor_key] = LAST_DIGIT
            return True

        if key in (curses.KEY_BACKSPACE, 127, 8):
            pos = prev_digit_before(cursor)
            text = text[:pos] + "0" + text[pos + 1:]
            state[text_key] = text
            state[cursor_key] = pos
            compute_results(state)
            return True

        if key in (curses.KEY_DC,):
            pos = cursor if cursor in DIGIT_SET else next_digit_pos(cursor)
            text = text[:pos] + "0" + text[pos + 1:]
            state[text_key] = text
            state[cursor_key] = pos
            compute_results(state)
            return True

        if ord("0") <= key <= ord("9"):
            ch = chr(key)
            pos = cursor if cursor in DIGIT_SET else next_digit_pos(cursor)
            text = text[:pos] + ch + text[pos + 1:]
            state[text_key] = text

            if state["focus_main"] == 1 and pos == LAST_DIGIT:
                state["focus_main"] = 2
                state["cursor_end"] = TIME_FIRST_DIGIT
            elif state["focus_main"] == 2 and pos == LAST_DIGIT:
                state["focus_main"] = 0
                state["from_list_open"] = False
            else:
                state[cursor_key] = next_digit_after(pos)
            compute_results(state)
            return True

    return False


# -----------------------------
# Mouse handling
# -----------------------------

def handle_mouse_main(state: dict, mx: int, my: int, bstate: int) -> bool:
    if bstate & curses.BUTTON4_PRESSED:
        state["results_scroll"] -= 3
        return True
    if bstate & curses.BUTTON5_PRESSED:
        state["results_scroll"] += 3
        return True

    if not (bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)):
        return False

    hit = region_hit(state.get("regions", []), mx, my)
    if not hit:
        if state.get("from_list_open"):
            state["from_list_open"] = False
            return True
        # Results click handling
        layout = state.get("results_layout")
        if not layout:
            return False
        row_start = layout["row_start"]
        visible = layout["visible"]
        row_step = layout.get("row_step", 1)
        start_idx = layout["start_idx"]
        if my < row_start or my >= row_start + (visible * row_step):
            return False
        offset = my - row_start
        if row_step > 1 and (offset % row_step) != 0:
            return False
        row_idx = start_idx + (offset // row_step)
        results = state.get("results") or []
        if not (0 <= row_idx < len(results)):
            return False
        tz_name, start_dt, end_dt = results[row_idx]
        zone_w = layout["zone_w"]
        dt_w = layout["dt_w"]
        table_x = layout.get("table_x", 0)
        zone_x = table_x + 1
        now_x = zone_x + zone_w + 1
        start_x = now_x + dt_w + 1
        end_x = start_x + dt_w + 1
        if zone_x <= mx < zone_x + zone_w:
            if tz_name in state["selected"]:
                state["from_idx"] = state["selected"].index(tz_name)
                state["focus_main"] = 1
                state["cursor_start"] = TIME_FIRST_DIGIT
                compute_results(state)
                return True
        if now_x <= mx < now_x + dt_w:
            if tz_name in state["selected"]:
                state["from_idx"] = state["selected"].index(tz_name)
            state["start_text"] = start_dt.strftime(INPUT_FMT)
            state["end_text"] = end_dt.strftime(INPUT_FMT)
            state["focus_main"] = 1
            state["cursor_start"] = TIME_FIRST_DIGIT
            state["from_list_open"] = False
            compute_results(state)
            return True
        if start_x <= mx < start_x + dt_w:
            state["start_text"] = start_dt.strftime(INPUT_FMT)
            state["focus_main"] = 1
            state["cursor_start"] = TIME_FIRST_DIGIT
            compute_results(state)
            return True
        if end_x <= mx < end_x + dt_w:
            state["end_text"] = end_dt.strftime(INPUT_FMT)
            state["focus_main"] = 2
            state["cursor_end"] = TIME_FIRST_DIGIT
            compute_results(state)
            return True
        return False

    action, payload = hit

    if action == "button_reset":
        reset_inputs(state)
        compute_results(state)
        return True
    if action == "button_settings":
        open_settings(state)
        return True
    if action == "button_quit":
        state["quit"] = True
        return False

    if action == "from_prev":
        if state["selected"]:
            state["from_idx"] = (state["from_idx"] - 1) % len(state["selected"])
            compute_results(state)
            return True
    if action == "from_next":
        if state["selected"]:
            state["from_idx"] = (state["from_idx"] + 1) % len(state["selected"])
            compute_results(state)
            return True
    if action == "from_toggle":
        if state["selected"]:
            state["from_list_open"] = not state["from_list_open"]
            state["from_list_idx"] = state["from_idx"]
            return True
    if action == "from_select" and isinstance(payload, int):
        if 0 <= payload < len(state["selected"]):
            state["from_idx"] = payload
            state["from_list_open"] = False
            state["focus_main"] = 1
            state["cursor_start"] = TIME_FIRST_DIGIT
            compute_results(state)
            return True
    if action == "focus_field" and isinstance(payload, tuple):
        field, value_start, _value_end = payload
        if field == "start":
            state["focus_main"] = 1
            rel = mx - value_start
            state["cursor_start"] = cursor_from_click(rel)
        else:
            state["focus_main"] = 2
            rel = mx - value_start
            state["cursor_end"] = cursor_from_click(rel)
        return True

    return False


def handle_mouse_settings(state: dict, mx: int, my: int, bstate: int) -> bool:
    boxes = state.get("settings_boxes", {})
    all_box = boxes.get("all")
    sel_box = boxes.get("selected")
    filter_box = boxes.get("filter")

    if bstate & curses.BUTTON4_PRESSED:
        if all_box and all_box[0] <= my < all_box[0] + all_box[2] and all_box[1] <= mx < all_box[1] + all_box[3]:
            state["settings_focus"] = 1
            state["all_scroll"] -= 1
            return True
        if sel_box and sel_box[0] <= my < sel_box[0] + sel_box[2] and sel_box[1] <= mx < sel_box[1] + sel_box[3]:
            state["settings_focus"] = 2
            state["sel_scroll"] -= 1
            return True
        return False

    if bstate & curses.BUTTON5_PRESSED:
        if all_box and all_box[0] <= my < all_box[0] + all_box[2] and all_box[1] <= mx < all_box[1] + all_box[3]:
            state["settings_focus"] = 1
            state["all_scroll"] += 1
            return True
        if sel_box and sel_box[0] <= my < sel_box[0] + sel_box[2] and sel_box[1] <= mx < sel_box[1] + sel_box[3]:
            state["settings_focus"] = 2
            state["sel_scroll"] += 1
            return True
        return False

    if not (bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)):
        return False

    hit = region_hit(state.get("regions", []), mx, my)
    if hit:
        action, _payload = hit
        if action == "settings_add":
            settings_add(state)
            return True
        if action == "settings_remove":
            settings_remove(state)
            return True
        if action == "settings_save":
            return save_settings(state)
        if action == "settings_cancel":
            cancel_settings(state)
            return True
        if action == "settings_box":
            current = state.get("settings_box_mode", "ascii")
            state["settings_box_mode"] = "unicode" if current == "ascii" else "ascii"
            return True
        if action == "settings_filter":
            state["settings_focus"] = 0
            state["filter_cursor"] = len(state["filter_text"])
            return True

    # Click inside panes
    if all_box and all_box[0] <= my < all_box[0] + all_box[2] and all_box[1] <= mx < all_box[1] + all_box[3]:
        state["settings_focus"] = 1
        idx = state["all_scroll"] + (my - all_box[0])
        if state["view_mode"] == 0:
            all_list = get_all_list(state)
            if 0 <= idx < len(all_list):
                state["all_idx"] = idx
        else:
            rows = build_country_rows(state["country_map"], state["filter_text"])
            if 0 <= idx < len(rows):
                if rows[idx].kind == "header":
                    state["all_idx"] = next_selectable_index(rows, idx, 1)
                else:
                    state["all_idx"] = idx
        return True

    if sel_box and sel_box[0] <= my < sel_box[0] + sel_box[2] and sel_box[1] <= mx < sel_box[1] + sel_box[3]:
        state["settings_focus"] = 2
        idx = state["sel_scroll"] + (my - sel_box[0])
        if 0 <= idx < len(state["settings_selected"]):
            state["sel_idx"] = idx
        return True

    if filter_box and filter_box[0] <= my < filter_box[0] + filter_box[2]:
        state["settings_focus"] = 0
        state["filter_cursor"] = len(state["filter_text"])
        return True

    return False


# -----------------------------
# Settings input
# -----------------------------

def handle_settings_input(key: int, state: dict) -> bool:
    if key == -1:
        return False

    if key == 9:  # Tab
        state["settings_focus"] = (state["settings_focus"] + 1) % 3
        return True

    if key in (curses.KEY_BTAB, 353):  # Shift+Tab
        state["settings_focus"] = (state["settings_focus"] - 1) % 3
        return True

    if key == 27:  # Esc
        cancel_settings(state)
        return True

    if state["settings_focus"] == 0:
        # Filter input
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if state["filter_cursor"] > 0:
                i = state["filter_cursor"]
                state["filter_text"] = state["filter_text"][: i - 1] + state["filter_text"][i:]
                state["filter_cursor"] -= 1
            return True
        if key in (curses.KEY_LEFT,):
            state["filter_cursor"] = max(0, state["filter_cursor"] - 1)
            return True
        if key in (curses.KEY_RIGHT,):
            state["filter_cursor"] = min(len(state["filter_text"]), state["filter_cursor"] + 1)
            return True
        if 32 <= key <= 126:
            ch = chr(key)
            i = state["filter_cursor"]
            state["filter_text"] = state["filter_text"][:i] + ch + state["filter_text"][i:]
            state["filter_cursor"] += 1
            state["all_idx"] = 0
            state["all_scroll"] = 0
            return True
        return False

    if key in (ord("q"), ord("Q")):
        cancel_settings(state)
        return True

    if key in (ord("c"), ord("C")):
        cancel_settings(state)
        return True

    if key in (ord("m"), ord("M")):
        set_mouse_enabled(state, not state.get("mouse_enabled"))
        return True

    if key in (ord("s"), ord("S")):
        return save_settings(state)

    if key in (ord("b"), ord("B")):
        current = state.get("settings_box_mode", "ascii")
        state["settings_box_mode"] = "unicode" if current == "ascii" else "ascii"
        return True

    if key in (ord("v"), ord("V")):
        if state.get("country_error"):
            state["settings_msg"] = "Country view unavailable."
            return True
        state["view_mode"] = (state["view_mode"] + 1) % len(VIEW_MODES)
        state["all_idx"] = 0
        state["all_scroll"] = 0
        return True

    if key in (ord("o"), ord("O")):
        if state["view_mode"] == 0:
            state["sort_mode"] = (state["sort_mode"] + 1) % len(SORT_MODES)
        else:
            state["settings_msg"] = "Sort modes only in flat view."
        return True

    if state["settings_focus"] == 1:
        if state["view_mode"] == 0:
            # All list, flat
            all_list = get_all_list(state)
            if key in (curses.KEY_UP,):
                state["all_idx"] = max(0, state["all_idx"] - 1)
                return True
            if key in (curses.KEY_DOWN,):
                state["all_idx"] = min(len(all_list) - 1, state["all_idx"] + 1)
                return True
            if key in (curses.KEY_NPAGE,):
                state["all_scroll"] += 3
                return True
            if key in (curses.KEY_PPAGE,):
                state["all_scroll"] -= 3
                return True
            if key in (curses.KEY_ENTER, 10, 13, ord("a"), ord("A")):
                settings_add(state)
                return True
        else:
            # All list, country view
            rows = build_country_rows(state["country_map"], state["filter_text"])
            if not rows:
                return False
            if key in (curses.KEY_UP,):
                state["all_idx"] = next_selectable_index(rows, state["all_idx"] - 1, -1)
                return True
            if key in (curses.KEY_DOWN,):
                state["all_idx"] = next_selectable_index(rows, state["all_idx"] + 1, 1)
                return True
            if key in (curses.KEY_NPAGE,):
                target = state["all_idx"] + (state["settings_boxes"]["all"][2] - 1)
                state["all_idx"] = next_selectable_index(rows, target, 1)
                return True
            if key in (curses.KEY_PPAGE,):
                target = state["all_idx"] - (state["settings_boxes"]["all"][2] - 1)
                state["all_idx"] = next_selectable_index(rows, target, -1)
                return True
            if key in (curses.KEY_ENTER, 10, 13, ord("a"), ord("A")):
                settings_add(state)
                return True

    if state["settings_focus"] == 2:
        # Selected list
        if key in (curses.KEY_UP,):
            state["sel_idx"] = max(0, state["sel_idx"] - 1)
            return True
        if key in (curses.KEY_DOWN,):
            state["sel_idx"] = min(len(state["settings_selected"]) - 1, state["sel_idx"] + 1)
            return True
        if key in (curses.KEY_NPAGE,):
            state["sel_scroll"] += 3
            return True
        if key in (curses.KEY_PPAGE,):
            state["sel_scroll"] -= 3
            return True
        if key in (ord("d"), ord("D"), curses.KEY_DC):
            settings_remove(state)
            return True
        if key in (ord("u"), ord("U")):
            settings_move_up(state)
            return True
        if key in (ord("j"), ord("J")):
            settings_move_down(state)
            return True

    return False


# -----------------------------
# Main loop
# -----------------------------

def main(stdscr: curses.window) -> None:
    stdscr.timeout(200)
    stdscr.keypad(True)

    all_zones_set = set(available_timezones())
    selected, box_mode = load_config(all_zones_set)

    country_map, country_err = load_country_timezones(all_zones_set)

    supported_unicode = unicode_supported(stdscr)

    state = {
        "selected": selected,
        "all_zones": sorted(all_zones_set),
        "from_idx": 0,
        "start_text": "",
        "end_text": "",
        "cursor_start": TIME_FIRST_DIGIT,
        "cursor_end": TIME_FIRST_DIGIT,
        "focus_main": 0,
        "from_list_open": False,
        "from_list_idx": 0,
        "from_list_scroll": 0,
        "results_scroll": 0,
        "results_layout": None,
        "results_row_start": 0,
        "error": "",
        "results": [],
        "duration_str": "",
        "total_minutes": 0,
        "quit": False,
        "regions": [],
        "mouse_enabled": False,
        "settings_open": False,
        "settings_selected": [],
        "settings_original": [],
        "settings_focus": 1,
        "filter_text": "",
        "filter_cursor": 0,
        "all_idx": 0,
        "all_scroll": 0,
        "sel_idx": 0,
        "sel_scroll": 0,
        "sort_mode": 0,
        "view_mode": 0,
        "settings_msg": "",
        "settings_boxes": {},
        "country_map": country_map,
        "country_error": country_err,
        "colors": False,
        "box_mode": "ascii",
        "box_style": BOX_STYLES["ascii"],
        "box_warning": "",
        "unicode_supported": supported_unicode,
        "settings_box_mode": "ascii",
    }

    init_colors(state)
    apply_box_mode(state, box_mode)
    set_mouse_enabled(state, True)
    reset_inputs(state)
    compute_results(state)

    last_tick = 0.0
    while not state["quit"]:
        now = time.monotonic()
        key = stdscr.getch()

        changed = False
        if key == curses.KEY_MOUSE and state.get("mouse_enabled"):
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                bstate = 0
            if state["settings_open"]:
                changed = handle_mouse_settings(state, mx, my, bstate)
            else:
                changed = handle_mouse_main(state, mx, my, bstate)
        else:
            if state["settings_open"]:
                changed = handle_settings_input(key, state)
            else:
                changed = handle_main_input(key, state)

        if int(now) != int(last_tick) or changed:
            last_tick = now
            if state["settings_open"]:
                render_settings(stdscr, state)
            else:
                render_main(stdscr, state)


def run() -> int:
    try:
        # Enable wide-char support in curses based on the current locale.
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        curses.wrapper(main)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
