#!/usr/bin/env python3
"""
Dynamic time zone TUI (curses + zoneinfo).

- ASCII-only UI
- Configurable list of selected time zones
- Settings modal with filter, add/remove, reorder, and sorting
"""

from __future__ import annotations

import curses
import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".pytzcvrt.json")
DEFAULT_SELECTED = [
    "Asia/Baghdad",
    "Europe/Stockholm",
    "America/Los_Angeles",
    "UTC",
]

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

# Mouse support (REPORT_MOUSE_POSITION may be missing on some terminals)
MOUSE_REPORT_POS = getattr(curses, "REPORT_MOUSE_POSITION", 0)
MOUSE_MASK = curses.ALL_MOUSE_EVENTS | MOUSE_REPORT_POS


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

def load_selected(all_zones: set[str]) -> list[str]:
    selected: list[str] = []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("selected"), list):
                for item in data["selected"]:
                    if isinstance(item, str) and item in all_zones:
                        selected.append(item)
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

    return selected


def save_selected(selected: list[str]) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"selected": selected}, f, indent=2)
    except Exception:
        pass


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
    max_len = w - x
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
    regions: list[tuple[int, int, int, int, str, object | None]],
    y: int,
    x: int,
    label: str,
    action: str,
) -> int:
    text = f"[{label}]"
    safe_addstr(stdscr, y, x, text)
    add_region(regions, y, x, y, x + len(text) - 1, action)
    return x + len(text) + 1


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


# -----------------------------
# Main screen render
# -----------------------------

def render_main(stdscr: curses.window, state: dict) -> None:
    stdscr.erase()
    regions: list[tuple[int, int, int, int, str, object | None]] = []
    state["regions"] = regions

    h, w = stdscr.getmaxyx()
    state["last_size"] = (h, w)
    if w < 80 or h < 20:
        safe_addstr(stdscr, 0, 0, "Window too small. Need at least 80x20.")
        safe_addstr(stdscr, 1, 0, f"Current size: {w}x{h}.")
        stdscr.refresh()
        return

    safe_addstr(stdscr, 0, 0, "pytzcvrt - dynamic time zone span converter")
    mouse_status = "on" if state.get("mouse_enabled") else "off"
    safe_addstr(
        stdscr,
        1,
        0,
        f"s=settings  r=reset  q=quit  Tab=next  m=mouse  Mouse: {mouse_status}",
    )

    # Header: show now for current From TZ
    if state["selected"]:
        from_name = state["selected"][state["from_idx"]]
        now_dt = datetime.now(ZoneInfo(from_name))
        safe_addstr(stdscr, 2, 0, f"Now ({from_name}): {format_dt_full(now_dt)}")
    else:
        safe_addstr(stdscr, 2, 0, "Now: (no selected zones)")

    # Compact selected list
    selected_line = ", ".join(state["selected"]) if state["selected"] else "(none)"
    safe_addstr(stdscr, 3, 0, f"Selected: {selected_line}")

    # Buttons
    line = 4
    x = 0
    x = draw_button(stdscr, regions, line, x, "Reset", "button_reset")
    x = draw_button(stdscr, regions, line, x, "Settings", "button_settings")
    x = draw_button(stdscr, regions, line, x, "Quit", "button_quit")

    # Span input
    line = 6
    safe_addstr(stdscr, line, 0, "Span input:")
    line += 1

    cursor_pos = None
    x = 0

    # From TZ field with [<] [>] and clickable value
    from_label = state["selected"][state["from_idx"]] if state["selected"] else "(none)"
    from_width = max(8, min(24, max((len(z) for z in state["selected"]), default=8)))

    from_label_text = "From TZ: "
    safe_addstr(stdscr, line, x, from_label_text)
    x += len(from_label_text)

    up_label = "[^]"
    safe_addstr(stdscr, line, x, up_label)
    add_region(regions, line, x, line, x + len(up_label) - 1, "from_prev")
    x += len(up_label) + 1

    value_text = (from_label + " " * from_width)[:from_width]
    value_attr = curses.A_REVERSE if state["focus_main"] == 0 else 0
    safe_addstr(stdscr, line, x, f"[{value_text}]", value_attr)
    add_region(regions, line, x, line, x + from_width + 1, "from_toggle")
    from_value_start = x + 1
    from_value_end = from_value_start + from_width - 1
    x += from_width + 3

    down_label = "[v]"
    safe_addstr(stdscr, line, x, down_label)
    add_region(regions, line, x, line, x + len(down_label) - 1, "from_next")
    x += len(down_label) + 2

    # Start field
    start_label_text = "Start: "
    start_field_start = x
    start_value_start = x + len(start_label_text) + 1
    start_value_end = start_value_start + INPUT_LEN - 1
    start_field_end = start_value_end + 1
    x, cur = draw_field(
        stdscr,
        line,
        x,
        "Start",
        state["start_text"],
        INPUT_LEN,
        state["focus_main"] == 1,
        state["cursor_start"],
    )
    add_region(
        regions,
        line,
        start_field_start,
        line,
        start_field_end,
        "focus_field",
        ("start", start_value_start, start_value_end),
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
    x, cur = draw_field(
        stdscr,
        line,
        x,
        "End",
        state["end_text"],
        INPUT_LEN,
        state["focus_main"] == 2,
        state["cursor_end"],
    )
    add_region(
        regions,
        line,
        end_field_start,
        line,
        end_field_end,
        "focus_field",
        ("end", end_value_start, end_value_end),
    )
    if cur:
        cursor_pos = cur

    # Optional dropdown for From TZ
    dropdown_info = None
    if state.get("from_list_open") and state["selected"]:
        dropdown_info = (from_value_start, line + 1)

    # Results panel
    line += 2
    safe_addstr(stdscr, line, 0, "Results (PgUp/PgDn or mouse wheel to scroll):")
    line += 1

    if state["error"]:
        safe_addstr(stdscr, line, 0, f"Error: {state['error']}")
        line += 1
    else:
        safe_addstr(
            stdscr,
            line,
            0,
            f"Duration: {state['duration_str']} ({state['total_minutes']} minutes)",
        )
        line += 1

    zone_w = min(28, max(12, w // 4))
    dt_w = max(19, (w - zone_w - 4) // 3)
    header = f"{'Zone':<{zone_w}} {'Now':<{dt_w}} {'Start':<{dt_w}} {'End':<{dt_w}}"
    safe_addstr(stdscr, line, 0, header)
    line += 1
    state["results_row_start"] = line

    results = state["results"] or []
    available = max(1, h - line - 1)
    state["results_scroll"] = clamp_scroll(state["results_scroll"], available, len(results))

    start_idx = state["results_scroll"]
    end_idx = min(len(results), start_idx + available)

    for idx in range(start_idx, end_idx):
        if line >= h:
            break
        tz_name, start_dt, end_dt = results[idx]
        now_dt = datetime.now(ZoneInfo(tz_name))
        tz_abbr = now_dt.tzname() or "UTC"
        tz_off = format_offset(now_dt.utcoffset(), with_colon=False)
        zone_label = f"{tz_name} {tz_abbr}{tz_off}"
        now_s = format_dt_local_seconds(now_dt)
        start_s = format_dt_local(start_dt)
        end_s = format_dt_local(end_dt)

        safe_addstr(stdscr, line, 0, f"{zone_label:<{zone_w}}")
        safe_addstr(stdscr, line, zone_w + 1, f"{now_s:<{dt_w}}")
        safe_addstr(stdscr, line, zone_w + 1 + dt_w + 1, f"{start_s:<{dt_w}}")
        safe_addstr(stdscr, line, zone_w + 1 + (dt_w + 1) * 2, f"{end_s:<{dt_w}}")
        line += 1

    # Draw dropdown overlay after results for true overlay
    if dropdown_info:
        dropdown_x, dropdown_y = dropdown_info
        list_h = h - dropdown_y - 1
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
        for i in range(start, end):
            label = state["selected"][i]
            item_y = dropdown_y + (i - start)
            item_text = f" {label} "
            attr = curses.A_REVERSE if i == state["from_list_idx"] else 0
            safe_addstr(stdscr, item_y, dropdown_x, item_text, attr)
            add_region(
                regions,
                item_y,
                dropdown_x,
                item_y,
                dropdown_x + len(item_text) - 1,
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

    safe_addstr(stdscr, 0, 0, "Settings (s=save, q=cancel, Tab=focus, o=sort)")
    safe_addstr(stdscr, 1, 0, f"Sort: {SORT_MODES[state['sort_mode']]}")

    # Filter line
    filter_label = "Filter: "
    safe_addstr(stdscr, 3, 0, filter_label)
    fx = len(filter_label)
    filter_text = state["filter_text"]
    safe_addstr(stdscr, 3, fx, f"[{filter_text}]")
    add_region(regions, 3, fx, 3, fx + len(filter_text) + 1, "settings_filter")

    # Panes
    list_y = 5
    list_h = h - list_y - 4
    list_h = max(3, list_h)
    gap = 3
    left_w = (w - gap) // 2
    right_w = w - gap - left_w
    left_x = 0
    right_x = left_w + gap

    # Headings
    left_title = "All time zones"
    right_title = "Selected time zones"
    safe_addstr(stdscr, list_y - 1, left_x, left_title)
    safe_addstr(stdscr, list_y - 1, right_x, right_title)

    # Build lists
    all_list = get_all_list(state)
    if not all_list:
        all_list = []
    selected_list = state["settings_selected"]

    # Clamp indices
    if all_list:
        state["all_idx"] = max(0, min(state["all_idx"], len(all_list) - 1))
    else:
        state["all_idx"] = 0
    if selected_list:
        state["sel_idx"] = max(0, min(state["sel_idx"], len(selected_list) - 1))
    else:
        state["sel_idx"] = 0

    state["all_scroll"] = ensure_visible(state["all_idx"], state["all_scroll"], list_h, len(all_list))
    state["sel_scroll"] = ensure_visible(state["sel_idx"], state["sel_scroll"], list_h, len(selected_list))

    # Draw All list
    for i in range(list_h):
        idx = state["all_scroll"] + i
        y = list_y + i
        if idx >= len(all_list):
            break
        name = all_list[idx]
        attr = 0
        if state["settings_focus"] == 1 and idx == state["all_idx"]:
            attr = curses.A_REVERSE
        safe_addstr(stdscr, y, left_x, name[: left_w - 1], attr)

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
        safe_addstr(stdscr, y, right_x, name[: right_w - 1], attr)

    # Store pane boxes for mouse hit-testing
    state["settings_boxes"] = {
        "all": (list_y, left_x, list_h, left_w),
        "selected": (list_y, right_x, list_h, right_w),
        "filter": (3, fx, 1, max(1, w - fx)),
    }

    # Buttons
    btn_y = h - 3
    x = 0
    x = draw_button(stdscr, regions, btn_y, x, "Add", "settings_add")
    x = draw_button(stdscr, regions, btn_y, x, "Remove", "settings_remove")
    x = draw_button(stdscr, regions, btn_y, x, "Save", "settings_save")
    x = draw_button(stdscr, regions, btn_y, x, "Cancel", "settings_cancel")

    # Status line
    status = state.get("settings_msg", "")
    safe_addstr(stdscr, h - 1, 0, status)

    # Cursor for filter
    if state["settings_focus"] == 0:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        cursor_x = fx + 1 + state["filter_cursor"]
        stdscr.move(3, min(cursor_x, w - 1))
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
    all_list = get_all_list(state)
    if not all_list:
        state["settings_msg"] = "No zones to add."
        return
    name = all_list[state["all_idx"]]
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
    save_selected(state["selected"])
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
        # Click in results pane to set fields
        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED):
            res = state.get("results") or []
            if res:
                h, w = state.get("last_size", (0, 0))
                # Reconstruct layout similar to render_main
                zone_w = min(28, max(12, w // 4)) if w else 20
                dt_w = max(19, (w - zone_w - 4) // 3) if w else 19
                # Compute where results start
                # Lines: 0 header,1 status,2 now,3 selected,4 buttons,5 blank,6 label,7 fields,8 blank,9 results label,10 duration,11 header
                # Render uses: results label at line+2 from span input, then duration line, then header.
                row_start = state.get("results_row_start", None)
                if row_start is None:
                    row_start = 12
                row_idx = (my - row_start) + state.get("results_scroll", 0)
                if 0 <= row_idx < len(res):
                    tz_name, start_dt, end_dt = res[row_idx]
                    # Column detection
                    if 0 <= mx < zone_w:
                        if tz_name in state["selected"]:
                            state["from_idx"] = state["selected"].index(tz_name)
                            state["focus_main"] = 1
                            state["cursor_start"] = TIME_FIRST_DIGIT
                            compute_results(state)
                            return True
                    now_x = zone_w + 1
                    start_x = zone_w + 1 + dt_w + 1
                    end_x = zone_w + 1 + (dt_w + 1) * 2
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
        field, value_start, value_end = payload
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
        if action == "settings_filter":
            state["settings_focus"] = 0
            state["filter_cursor"] = len(state["filter_text"])
            return True

    # Click inside panes
    if all_box and all_box[0] <= my < all_box[0] + all_box[2] and all_box[1] <= mx < all_box[1] + all_box[3]:
        state["settings_focus"] = 1
        idx = state["all_scroll"] + (my - all_box[0])
        all_list = get_all_list(state)
        if 0 <= idx < len(all_list):
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

    if key in (ord("m"), ord("M")):
        set_mouse_enabled(state, not state.get("mouse_enabled"))
        return True

    if key in (ord("s"), ord("S")):
        return save_settings(state)

    if key in (ord("o"), ord("O")):
        state["sort_mode"] = (state["sort_mode"] + 1) % len(SORT_MODES)
        return True

    if state["settings_focus"] == 1:
        # All list
        if key in (curses.KEY_UP,):
            state["all_idx"] = max(0, state["all_idx"] - 1)
            return True
        if key in (curses.KEY_DOWN,):
            state["all_idx"] = state["all_idx"] + 1
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

    if state["settings_focus"] == 2:
        # Selected list
        if key in (curses.KEY_UP,):
            state["sel_idx"] = max(0, state["sel_idx"] - 1)
            return True
        if key in (curses.KEY_DOWN,):
            state["sel_idx"] = state["sel_idx"] + 1
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
    selected = load_selected(all_zones_set)

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
        "settings_msg": "",
        "settings_boxes": {},
    }

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
        curses.wrapper(main)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
