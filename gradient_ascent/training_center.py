from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any

from .activity_titles import is_placeholder_title, select_activity_title
from .coach_notes import coach_notes_by_date
from .dashboard_labels import day_labels_by_date, ride_annotations_by_id
from .planned_load import (
    MAX_DAILY_HOURS,
    MAX_DAILY_TSS,
    day_planned_load,
    parse_source_range,
    structured_workout_load,
    week_planned_load,
)
from .planned_workouts import load_structured_workouts
from .progress import build_progress_artifact
from .storage import ensure_text_line, read_json, write_json, write_text
from .tss_budgets import load_tss_budgets
from .workspace_lock import cross_process_locking_available, workspace_lock


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ACTIVITY_DETAILS_CACHE_VERSION = 6
MAX_ACTIVITY_DETAIL_SIDECAR_BYTES = 64 * 1024 * 1024
_training_center_build_lock = workspace_lock
WEEKDAY_NAMES = {
    "Mon": "monday",
    "Tue": "tuesday",
    "Wed": "wednesday",
    "Thu": "thursday",
    "Fri": "friday",
    "Sat": "saturday",
    "Sun": "sunday",
}


def _ensure_private_output_directory(data_dir: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Training Center output directory must stay inside the workspace.")
    data_root = data_dir.expanduser().resolve()
    target = data_root / relative
    current = data_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"Training Center output directory cannot be a symlink: {current}"
            )
    target.mkdir(parents=True, exist_ok=True)
    try:
        target.resolve().relative_to(data_root)
    except ValueError as exc:
        raise ValueError(
            "Training Center output directory must stay inside the workspace."
        ) from exc
    try:
        target.chmod(0o700)
    except OSError:
        pass
    return target


def _weekday_value(data: dict[str, Any], weekday: str) -> Any:
    for key in (weekday, WEEKDAY_NAMES[weekday].title()):
        value = data.get(key)
        if value not in (None, ""):
            return value
    short = weekday.lower()
    long = WEEKDAY_NAMES[weekday]
    for key, value in data.items():
        normalized = re.sub(r"\s+", " ", str(key).strip().lower())
        if normalized.startswith(f"{short} (") or normalized.startswith(f"{long} ("):
            if value not in (None, ""):
                return value
    return None


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#fffdf9"/>
  <text
    x="32"
    y="46"
    fill="#2F6F4E"
    font-family="Arial, sans-serif"
    font-size="46"
    font-style="italic"
    font-weight="800"
    text-anchor="middle"
  >G</text>
</svg>
"""


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'" />
  <title>Gradient Ascent Training Center</title>
  <link rel="icon" type="image/svg+xml" href="training_center_favicon.svg" />
  <style>
    :root {
      --bg: #111610;
      --panel: rgba(28, 34, 28, 0.78);
      --panel-strong: rgba(39, 47, 39, 0.9);
      --line: rgba(221, 222, 204, 0.13);
      --line-strong: rgba(168, 199, 160, 0.36);
      --text: #eeeade;
      --muted: #a8aa9c;
      --accent: #a8c7a0;
      --accent-soft: rgba(168, 199, 160, 0.13);
      --gold: #d4bf88;
      --warning: #d29a8f;
      --blue: #93b8d8;
      --shadow: 0 18px 38px rgba(5, 8, 5, 0.32);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      scroll-padding-top: 132px;
      background:
        radial-gradient(circle at 12% 0%, rgba(168, 199, 160, 0.15), transparent 34%),
        radial-gradient(circle at 86% 8%, rgba(212, 191, 136, 0.12), transparent 30%),
        linear-gradient(145deg, #0d100c 0%, var(--bg) 48%, #20271f 100%);
    }

    button, select, textarea, input {
      font: inherit;
    }

    button, .ghost-link {
      border: 1px solid rgba(168, 199, 160, 0.22);
      border-radius: 999px;
      background: rgba(168, 199, 160, 0.08);
      color: var(--text);
      padding: 9px 13px;
      cursor: pointer;
      text-decoration: none;
      transition: border-color 0.12s ease, transform 0.12s ease, background 0.12s ease;
    }

    button:hover, .ghost-link:hover {
      transform: translateY(-1px);
      border-color: var(--line-strong);
      background: rgba(168, 199, 160, 0.13);
    }

    button.active {
      border-color: rgba(212, 191, 136, 0.5);
      color: var(--gold);
      background: rgba(212, 191, 136, 0.1);
    }

    .page {
      width: min(1680px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0 36px;
    }

    .eyebrow, .meta, .tab, .pill, .month-name, .weekday, .stat-label {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      text-transform: uppercase;
      letter-spacing: 0.11em;
    }

    .eyebrow {
      color: var(--gold);
      font-size: 0.78rem;
      margin: 0 0 10px;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 30;
      display: grid;
      grid-template-columns: minmax(170px, 1fr) auto auto;
      gap: 12px;
      align-items: center;
      margin-bottom: 18px;
      padding: 12px 14px;
      border: 1px solid rgba(221, 222, 204, 0.12);
      border-radius: 24px;
      background: rgba(15, 19, 14, 0.9);
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }

    .brand {
      min-width: 0;
    }

    .topbar-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
      min-width: 0;
      position: relative;
    }

    .topbar-actions .coach-conversation-button {
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 8px 13px;
      border: 1px solid #2f6f4e;
      border-radius: 999px;
      background: #2f6f4e;
      color: #fff;
      font-size: 0.74rem;
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
    }

    .topbar-actions .coach-conversation-button:hover,
    .topbar-actions .coach-conversation-button:focus-visible {
      border-color: #24573d;
      background: #24573d;
    }

    .tabs {
      display: flex;
      gap: 10px;
      flex-wrap: nowrap;
      margin: 0;
    }

    .tab {
      font-size: 0.75rem;
      font-weight: 700;
    }

    .icon-button {
      width: 40px;
      min-width: 40px;
      height: 40px;
      display: inline-grid;
      place-items: center;
      padding: 0;
    }

    .icon-button svg {
      width: 17px;
      height: 17px;
      fill: none;
      stroke: currentColor;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 2;
    }

    .sync-button svg {
      transition: transform 160ms ease;
    }

    .sync-button.sync-running svg {
      animation: sync-spin 800ms linear infinite;
    }

    @keyframes sync-spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }

    .action-menu-wrap {
      position: relative;
    }

    .action-menu {
      position: absolute;
      z-index: 20;
      top: calc(100% + 8px);
      right: 0;
      width: 232px;
      display: grid;
      gap: 4px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(15, 19, 14, 0.98);
      box-shadow: var(--shadow);
    }

    .action-menu[hidden] {
      display: none;
    }

    .action-menu button {
      width: 100%;
      min-height: 36px;
      display: flex;
      justify-content: flex-start;
      align-items: center;
      padding: 8px 10px;
      border: 0;
      border-radius: 6px;
      color: var(--text);
      background: transparent;
      font: inherit;
      text-align: left;
    }

    .action-menu button:hover,
    .action-menu button:focus-visible {
      color: var(--text);
      background: rgba(168, 199, 160, 0.12);
    }

    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }

    .context-stack {
      display: grid;
      gap: 12px;
      min-width: 0;
    }

    .panel {
      border: 1px solid var(--line);
      border-radius: 28px;
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }

    .view {
      display: none;
      min-height: 72vh;
    }

    .view.active {
      display: block;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }

    .toolbar > div {
      min-width: 0;
    }

    .toolbar h2 {
      margin: 0;
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      white-space: nowrap;
    }

    .toolbar-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: flex-end;
      min-width: 0;
    }

    .week-toolbar-actions {
      margin-left: auto;
      justify-content: flex-end;
    }

    .toolbar select,
    .toolbar input[type="date"] {
      width: min(420px, 100%);
      min-width: 0;
      max-width: 100%;
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(13, 16, 12, 0.72);
      padding: 9px 12px;
    }

    .today-toolbar-actions input[type="date"] {
      width: auto;
      min-width: 170px;
    }

    button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none;
    }

    .sync-button.sync-running {
      color: var(--gold);
      border-color: rgba(212, 191, 136, 0.45);
      background: rgba(212, 191, 136, 0.12);
    }

    .calendar-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      padding: 18px;
    }

    .month-card {
      border: 1px solid var(--line);
      border-radius: 22px;
      background:
        radial-gradient(circle at 0% 0%, rgba(168, 199, 160, 0.105), transparent 34%),
        rgba(238, 234, 222, 0.03);
      padding: 16px;
      min-width: 0;
    }

    .month-name-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin: 0 0 14px;
    }

    .month-name {
      margin: 0;
      color: var(--accent);
      font-size: 0.78rem;
    }

    .signal-key {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
      color: rgba(238, 234, 222, 0.68);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.56rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .signal-key span {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }

    .key-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 0 3px rgba(168, 199, 160, 0.12);
    }

    .key-dot.big {
      background: #c9a66a;
      box-shadow: 0 0 0 3px rgba(201, 166, 106, 0.13);
    }

    .key-dot.race-road {
      background: var(--blue);
      box-shadow: 0 0 0 3px rgba(147, 184, 216, 0.14);
    }

    .key-dot.race-dirt {
      background: var(--warning);
      box-shadow: 0 0 0 3px rgba(210, 154, 143, 0.14);
    }

    .month-weekdays, .month-week-row {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr)) minmax(146px, 0.6fr);
      gap: 7px;
    }

    .month-rows {
      display: grid;
      gap: 7px;
    }

    .weekday {
      color: var(--muted);
      font-size: 0.62rem;
      text-align: center;
    }

    .week-total-heading {
      text-align: left;
    }

    .blank-day {
      min-height: clamp(106px, 7.6vw, 146px);
      border-radius: 14px;
      background: rgba(9, 12, 8, 0.18);
    }

    .calendar-day {
      position: relative;
      min-width: 0;
      min-height: clamp(106px, 7.6vw, 146px);
      border: 1px solid rgba(238, 234, 222, 0.09);
      border-radius: 14px;
      background: rgba(16, 19, 15, 0.5);
      color: var(--text);
      padding: 10px;
      text-align: left;
      display: grid;
      align-content: start;
      gap: 6px;
      overflow: hidden;
    }

    .calendar-day::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      opacity: 0;
      background: var(--accent);
    }

    .calendar-day.today {
      border-color: rgba(212, 191, 136, 0.45);
    }

    .calendar-day.selected {
      border-color: var(--line-strong);
      background: rgba(168, 199, 160, 0.1);
    }

    .calendar-day.has-ride {
      box-shadow: inset 0 -3px 0 rgba(168, 199, 160, 0.28);
    }

    .calendar-day.has-event {
      box-shadow: inset 0 3px 0 rgba(212, 191, 136, 0.34);
    }

    .calendar-day.interval-day {
      border-color: rgba(168, 199, 160, 0.34);
      background:
        linear-gradient(180deg, rgba(168, 199, 160, 0.11), rgba(16, 19, 15, 0.5) 42%),
        rgba(16, 19, 15, 0.5);
    }

    .calendar-day.interval-day::before {
      opacity: 0.8;
      background: var(--accent);
    }

    .calendar-day.race-day {
      border-color: rgba(212, 191, 136, 0.48);
      background:
        linear-gradient(180deg, rgba(212, 191, 136, 0.12), rgba(16, 19, 15, 0.52) 46%),
        rgba(16, 19, 15, 0.52);
      box-shadow: inset 0 0 0 1px rgba(212, 191, 136, 0.08), 0 12px 28px rgba(6, 7, 5, 0.2);
    }

    .calendar-day.race-day::before {
      opacity: 1;
      background: var(--gold);
    }

    .calendar-day.race-road-day {
      border-color: rgba(147, 184, 216, 0.62);
      background:
        linear-gradient(180deg, rgba(147, 184, 216, 0.18), rgba(16, 19, 15, 0.52) 48%),
        rgba(16, 19, 15, 0.52);
      box-shadow: inset 0 0 0 1px rgba(147, 184, 216, 0.12), 0 12px 28px rgba(6, 7, 5, 0.2);
    }

    .calendar-day.race-road-day::before {
      background: var(--blue);
    }

    .calendar-day.race-dirt-day {
      border-color: rgba(210, 154, 143, 0.64);
      background:
        linear-gradient(180deg, rgba(210, 154, 143, 0.18), rgba(16, 19, 15, 0.52) 48%),
        rgba(16, 19, 15, 0.52);
      box-shadow: inset 0 0 0 1px rgba(210, 154, 143, 0.12), 0 12px 28px rgba(6, 7, 5, 0.2);
    }

    .calendar-day.race-dirt-day::before {
      background: var(--warning);
    }

    .calendar-day.big-day:not(.race-day) {
      border-color: rgba(201, 166, 106, 0.42);
      background:
        linear-gradient(180deg, rgba(201, 166, 106, 0.14), rgba(16, 19, 15, 0.5) 44%),
        rgba(16, 19, 15, 0.5);
      box-shadow: inset 0 -4px 0 rgba(201, 166, 106, 0.5);
    }

    .calendar-day.race-day.big-day,
    .calendar-day.race-day.has-event,
    .calendar-day.race-day.has-ride {
      box-shadow: inset 0 -4px 0 rgba(201, 166, 106, 0.5), inset 0 0 0 1px rgba(212, 191, 136, 0.1), 0 12px 28px rgba(6, 7, 5, 0.22);
    }

    .calendar-day.interval-day.big-day:not(.race-day) {
      border-color: rgba(168, 199, 160, 0.38);
      background:
        linear-gradient(180deg, rgba(168, 199, 160, 0.12), rgba(16, 19, 15, 0.5) 42%),
        rgba(16, 19, 15, 0.5);
      box-shadow: inset 0 -4px 0 rgba(201, 166, 106, 0.48);
    }

    .calendar-day.selected {
      outline: 1px solid var(--line-strong);
      outline-offset: -3px;
    }

    .day-number {
      font-weight: 700;
      font-size: 0.92rem;
    }

    .day-mini {
      color: rgba(238, 234, 222, 0.76);
      font-size: 0.86rem;
      line-height: 1.32;
      overflow: hidden;
      overflow-wrap: anywhere;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
    }

    .day-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      min-height: 17px;
    }

    .day-tag {
      width: fit-content;
      border: 1px solid rgba(238, 234, 222, 0.12);
      border-radius: 999px;
      color: rgba(238, 234, 222, 0.78);
      background: rgba(238, 234, 222, 0.045);
      padding: 2px 5px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.53rem;
      letter-spacing: 0.06em;
      line-height: 1.2;
      text-transform: uppercase;
    }

    .day-tag.interval {
      color: var(--accent);
      border-color: rgba(168, 199, 160, 0.28);
      background: rgba(168, 199, 160, 0.08);
    }

    .day-tag.big {
      color: var(--gold);
      border-color: rgba(212, 191, 136, 0.3);
      background: rgba(212, 191, 136, 0.085);
    }

    .day-tag.custom {
      color: var(--blue);
      border-color: rgba(147, 184, 216, 0.34);
      background: rgba(147, 184, 216, 0.09);
    }

    .day-tag.reaction {
      color: var(--text);
      border-color: rgba(238, 234, 222, 0.18);
      background: rgba(238, 234, 222, 0.08);
      text-transform: none;
    }

    .day-tag.race-road {
      color: var(--blue);
      border-color: rgba(147, 184, 216, 0.34);
      background: rgba(147, 184, 216, 0.09);
    }

    .day-tag.race-dirt {
      color: var(--warning);
      border-color: rgba(210, 154, 143, 0.34);
      background: rgba(210, 154, 143, 0.09);
    }

    .day-tag.coach-note {
      color: #d7cfaa;
      border-color: rgba(215, 207, 170, 0.34);
      background: rgba(215, 207, 170, 0.08);
    }

    .day-kpi {
      margin-top: auto;
      color: rgba(238, 234, 222, 0.56);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.58rem;
      line-height: 1.35;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .calendar-week-stat {
      width: 100%;
      min-width: 0;
      min-height: clamp(106px, 7.6vw, 146px);
      border-radius: 14px;
      padding: 10px;
      border-color: rgba(168, 199, 160, 0.16);
      background: rgba(168, 199, 160, 0.052);
      display: grid;
      align-content: start;
      gap: 5px;
      text-align: left;
    }

    .calendar-week-stat strong {
      font-size: 1.02rem;
      line-height: 1;
    }

    .calendar-week-stat span {
      color: rgba(238, 234, 222, 0.72);
      font-size: 0.72rem;
      line-height: 1.25;
    }

    .calendar-week-stat .stat-label {
      color: var(--accent);
      font-size: 0.58rem;
    }

    .week-stat-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
    }

    .month-summary-card {
      min-width: 0;
      border: 1px solid rgba(168, 199, 160, 0.15);
      border-radius: 16px;
      background:
        linear-gradient(90deg, rgba(168, 199, 160, 0.1), rgba(12, 16, 12, 0.48)),
        rgba(12, 16, 12, 0.5);
      padding: 11px 12px;
      display: flex;
      align-items: center;
      gap: 14px;
      margin: 0 0 13px;
    }

    .month-summary-card .eyebrow {
      margin: 0;
      font-size: 0.64rem;
      min-width: max-content;
    }

    .month-summary-main {
      display: flex;
      align-items: baseline;
      gap: 7px;
      min-width: max-content;
    }

    .month-summary-main strong {
      font-size: clamp(1.35rem, 2vw, 2rem);
      line-height: 0.9;
      letter-spacing: -0.07em;
    }

    .month-summary-main span,
    .month-summary-line span {
      color: rgba(238, 234, 222, 0.62);
      font-size: 0.74rem;
      line-height: 1.35;
    }

    .month-summary-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 7px;
      flex: 1 1 260px;
    }

    .month-summary-stat {
      min-width: 0;
      border: 1px solid rgba(238, 234, 222, 0.075);
      border-radius: 13px;
      background: rgba(238, 234, 222, 0.035);
      padding: 9px;
    }

    .month-summary-stat strong {
      display: inline;
      font-size: 1rem;
      line-height: 1;
    }

    .month-summary-stat span {
      color: rgba(238, 234, 222, 0.58);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.55rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .month-summary-line {
      display: grid;
      gap: 4px;
      min-width: max-content;
    }

    .month-summary-line strong {
      color: var(--accent);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.62rem;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }

    .week-list {
      padding: 18px;
      display: grid;
      gap: 18px;
    }

    .connection-layout {
      display: grid;
      gap: 18px;
      padding: 18px;
    }

    .connection-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }

    .connection-section {
      display: grid;
      gap: 10px;
    }

    .connection-section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
    }

    .connection-section-head h3 {
      margin: 0;
      font-size: 0.78rem;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }

    .connection-section-head span {
      color: var(--muted);
      font-size: 0.78rem;
    }

    .connection-future {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(16, 19, 15, 0.34);
      padding: 12px;
    }

    .connection-future > summary {
      cursor: pointer;
      color: var(--muted);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.72rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .connection-future[open] > summary {
      margin-bottom: 12px;
    }

    .connection-card {
      display: grid;
      gap: 12px;
      border: 1px solid rgba(238, 234, 222, 0.09);
      border-radius: 18px;
      background: rgba(16, 19, 15, 0.5);
      padding: 16px;
    }

    .connection-card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }

    .connection-card h3 {
      margin: 0 0 4px;
      font-size: 1.08rem;
    }

    .connection-badges {
      display: grid;
      gap: 6px;
      justify-items: end;
    }

    .connection-copy,
    .connection-note,
    .connection-steps {
      margin: 0;
      color: rgba(238, 234, 222, 0.76);
      line-height: 1.45;
      font-size: 0.88rem;
    }

    .connection-status {
      border: 1px solid rgba(238, 234, 222, 0.16);
      border-radius: 999px;
      padding: 6px 8px;
      color: var(--muted);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.62rem;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .connection-status.connected {
      border-color: rgba(168, 199, 160, 0.34);
      color: var(--accent);
    }

    .connection-status.imported {
      border-color: rgba(168, 199, 160, 0.34);
      color: var(--accent);
    }

    .connection-status.configured {
      border-color: rgba(212, 191, 136, 0.34);
      color: var(--gold);
    }

    .connection-status.blocked {
      border-color: rgba(215, 146, 120, 0.34);
      color: #d79278;
    }

    .connection-tier {
      display: inline-flex;
      width: fit-content;
      border-radius: 999px;
      border: 1px solid rgba(238, 234, 222, 0.14);
      color: rgba(238, 234, 222, 0.62);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.61rem;
      padding: 5px 7px;
      text-transform: uppercase;
    }

    .connection-tier.official {
      border-color: rgba(168, 199, 160, 0.34);
      color: var(--accent);
    }

    .connection-form {
      display: grid;
      gap: 10px;
    }

    .connection-field {
      display: grid;
      gap: 5px;
    }

    .connection-field span {
      color: rgba(238, 234, 222, 0.56);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.64rem;
      text-transform: uppercase;
    }

    .connection-field input {
      width: 100%;
      min-width: 0;
      border: 1px solid rgba(238, 234, 222, 0.14);
      border-radius: 10px;
      background: rgba(7, 9, 6, 0.46);
      color: var(--paper);
      padding: 10px 11px;
      font: inherit;
    }

    .connection-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .connection-actions button,
    .connection-actions a {
      border: 1px solid rgba(238, 234, 222, 0.14);
      border-radius: 10px;
      background: rgba(238, 234, 222, 0.04);
      color: var(--paper);
      padding: 9px 11px;
      font: inherit;
      text-decoration: none;
    }

    .connection-actions button:hover,
    .connection-actions a:hover {
      background: rgba(238, 234, 222, 0.08);
    }

    .connection-actions button.primary,
    .connection-actions a.primary {
      border-color: rgba(168, 199, 160, 0.28);
      color: var(--accent);
    }

    .connection-setup-status {
      border-left: 2px solid var(--accent);
      padding-left: 12px;
      overflow-wrap: anywhere;
    }

    .connection-actions button:disabled {
      cursor: wait;
      opacity: 0.55;
      transform: none;
    }

    .empty-state-actions a,
    .empty-state-actions button {
      border-color: #27473f;
      background: #27473f;
      color: #fffdf9;
      cursor: pointer;
    }

    .empty-state-actions a:hover,
    .empty-state-actions button:hover {
      background: #1f3933;
    }

    .connection-archive {
      display: grid;
      gap: 10px;
      border: 1px solid rgba(168, 199, 160, 0.2);
      border-radius: 12px;
      background: rgba(168, 199, 160, 0.06);
      padding: 12px;
    }

    .connection-upload {
      display: grid;
      gap: 6px;
      color: rgba(238, 234, 222, 0.7);
      font-size: 0.82rem;
    }

    .connection-upload input {
      width: 100%;
      color: var(--text);
    }

    .recording-drop-overlay {
      position: fixed;
      inset: 18px;
      z-index: 1000;
      display: grid;
      place-items: center;
      border: 2px dashed rgba(202, 224, 195, 0.78);
      border-radius: 30px;
      background: rgba(14, 23, 18, 0.94);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.58);
      backdrop-filter: blur(18px);
      pointer-events: none;
    }

    .recording-drop-overlay[hidden] {
      display: none;
    }

    .recording-drop-card {
      display: grid;
      justify-items: center;
      gap: 10px;
      max-width: 480px;
      padding: 34px;
      text-align: center;
    }

    .recording-drop-card strong {
      color: #f4f1e8;
      font-size: clamp(1.35rem, 3vw, 2.35rem);
      letter-spacing: -0.04em;
    }

    .recording-drop-card span {
      color: rgba(244, 241, 232, 0.76);
      line-height: 1.5;
    }

    .connection-empty {
      border: 1px dashed rgba(238, 234, 222, 0.16);
      border-radius: 18px;
      padding: 18px;
      color: var(--muted);
    }

    .settings-layout {
      display: grid;
      gap: 14px;
      padding: 18px;
      max-width: 560px;
    }

    .setting-row {
      display: grid;
      gap: 8px;
    }

    .setting-row span {
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .setting-row select {
      width: min(320px, 100%);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(13, 16, 12, 0.72);
      padding: 9px 12px;
    }

    .setting-toggle {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      width: fit-content;
      color: var(--text);
    }

    .setting-toggle input {
      width: 16px;
      height: 16px;
      margin: 0;
    }

    .week-card {
      border: 1px solid var(--line);
      border-radius: 24px;
      background: rgba(238, 234, 222, 0.035);
      padding: 18px;
    }

    .week-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 14px;
      align-items: start;
      margin-bottom: 16px;
    }

    .week-overview {
      display: grid;
      gap: 14px;
      margin: 0 0 18px;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }

    .summary-card {
      border: 1px solid rgba(238, 234, 222, 0.09);
      border-radius: 18px;
      background: rgba(16, 19, 15, 0.42);
      padding: 13px;
    }

    .summary-value {
      font-size: 1.45rem;
      line-height: 1;
      font-weight: 700;
      letter-spacing: -0.04em;
      margin-bottom: 7px;
    }

    .progress-row {
      display: grid;
      gap: 8px;
    }

    .progress-label {
      color: var(--muted);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.72rem;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }

    .progress-track {
      position: relative;
      height: 14px;
      border: 1px solid rgba(238, 234, 222, 0.09);
      border-radius: 999px;
      background: rgba(7, 9, 6, 0.4);
      overflow: hidden;
    }

    .progress-fill {
      width: var(--progress, 0%);
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, rgba(168, 199, 160, 0.56), rgba(212, 191, 136, 0.42));
    }

    .progress-min {
      position: absolute;
      left: var(--target-min, 0%);
      top: 0;
      width: 2px;
      height: 100%;
      background: rgba(212, 191, 136, 0.72);
    }

    .progress-frame {
      display: block;
      width: 100%;
      min-height: 72vh;
      border: 0;
      background: transparent;
    }

    .week-detail, .plan-detail {
      margin: 0;
      color: rgba(238, 234, 222, 0.76);
      line-height: 1.45;
      font-size: 0.9rem;
    }

    .week-title {
      margin: 0 0 6px;
      font-size: 1.45rem;
      letter-spacing: -0.04em;
    }

    .week-focus {
      margin: 0;
      color: rgba(238, 234, 222, 0.78);
      line-height: 1.45;
    }

    .pill-row, .event-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }

    .pill {
      border: 1px solid rgba(168, 199, 160, 0.2);
      border-radius: 999px;
      color: var(--accent);
      background: rgba(168, 199, 160, 0.08);
      font-size: 0.66rem;
      padding: 7px 9px;
    }

    .pill.warn {
      border-color: rgba(212, 191, 136, 0.28);
      color: var(--gold);
      background: rgba(212, 191, 136, 0.08);
    }

    .week-days {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 10px;
    }

    .week-day {
      min-width: 0;
      overflow: hidden;
      border: 1px solid rgba(238, 234, 222, 0.09);
      border-radius: 18px;
      background: rgba(16, 19, 15, 0.5);
      padding: 12px;
      display: flex;
      flex-direction: column;
      height: 304px;
      gap: 9px;
      cursor: pointer;
    }

    .week-day.selected {
      border-color: var(--line-strong);
    }

    .week-day:focus-visible {
      outline: 2px solid rgba(168, 199, 160, 0.28);
      outline-offset: 2px;
    }

    .week-day.has-ride {
      box-shadow: inset 0 -3px 0 rgba(168, 199, 160, 0.22);
    }

    .week-day.hard-day {
      background: rgba(212, 191, 136, 0.055);
    }

    .week-day-head {
      display: block;
    }

    .week-day-signals {
      display: flex;
      flex-wrap: nowrap;
      justify-content: flex-start;
      gap: 4px;
      min-width: 0;
      overflow: hidden;
      border: 0;
      background: transparent;
      padding: 0;
      white-space: nowrap;
    }

    .week-day-signals:hover {
      transform: none;
      background: transparent;
    }

    .week-day-signal {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 24px;
      min-height: 22px;
      padding: 3px 7px;
      font-size: 0.62rem;
      line-height: 1;
      text-align: center;
    }

    .week-day-signals:not(.expanded) .week-day-signal {
      min-width: 0;
      padding-inline: 6px;
    }

    .week-day-signals.expanded {
      flex-wrap: wrap;
      gap: 5px;
      overflow: visible;
      white-space: normal;
    }

    .week-day-signals.expanded .week-day-signal {
      width: fit-content;
      min-width: 0;
      min-height: 22px;
      padding: 3px 7px;
    }

    .week-day-signal.interval {
      background: rgba(168, 199, 160, 0.58);
    }

    .week-day-signal.big {
      background: rgba(212, 191, 136, 0.62);
    }

    .week-day-signal.race-road {
      background: rgba(147, 184, 216, 0.7);
    }

    .week-day-signal.race-dirt {
      background: rgba(215, 146, 120, 0.68);
    }

    .week-day-signal.coach-note {
      background: rgba(212, 191, 136, 0.62);
    }

    .date-label {
      font-weight: 700;
    }

    .planned, .actual, .metric-line, .empty-ride-stats {
      margin: 0;
      line-height: 1.35;
      font-size: 0.88rem;
      overflow-wrap: anywhere;
    }

    .planned {
      font-weight: 700;
    }

    .actual, .metric-line, .empty-ride-stats {
      color: rgba(238, 234, 222, 0.72);
    }

    .actual-link {
      color: inherit;
      text-decoration-color: rgba(168, 199, 160, 0.42);
      text-underline-offset: 3px;
    }

    .actual-link:hover {
      color: var(--accent);
    }

    .week-ride-stats,
    .ride-assessment-stats {
      display: grid;
      gap: 8px;
      min-width: 0;
    }

    .week-day-title-stack,
    .week-day-meta {
      display: grid;
      gap: 6px;
      min-width: 0;
    }

    .week-day-strava {
      margin: 0;
      font-size: 0.76rem;
      line-height: 1.2;
    }

    .week-day-footer {
      margin-top: auto;
    }

    .week-stat-chip-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
    }

    .ride-stat-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 7px;
    }

    .week-stat-chip,
    .ride-stat-chip {
      min-width: 0;
      border: 1px solid rgba(168, 199, 160, 0.13);
      border-radius: 13px;
      background: rgba(168, 199, 160, 0.055);
    }

    .week-stat-chip {
      padding: 7px;
    }

    .ride-stat-chip {
      padding: 8px;
    }

    .week-stat-chip strong,
    .ride-stat-chip strong {
      display: block;
      overflow: hidden;
      color: var(--text);
      font-size: 0.92rem;
      line-height: 1.05;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .week-stat-chip span,
    .ride-stat-chip span {
      display: block;
      margin-top: 4px;
      color: rgba(238, 234, 222, 0.56);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.54rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .interval-list {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      min-width: 0;
    }

    .interval-chip {
      border: 1px solid rgba(212, 191, 136, 0.2);
      border-radius: 999px;
      color: var(--gold);
      background: rgba(212, 191, 136, 0.07);
      padding: 4px 7px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.56rem;
      letter-spacing: 0.07em;
      text-transform: uppercase;
    }

    .interval-chip.quiet {
      color: rgba(238, 234, 222, 0.46);
      border-color: rgba(238, 234, 222, 0.08);
      background: rgba(238, 234, 222, 0.035);
    }

    .week-interval-list {
      flex-wrap: nowrap;
      align-items: center;
      overflow: hidden;
      white-space: nowrap;
    }

    .week-interval-list .interval-chip {
      flex: 0 0 auto;
      white-space: nowrap;
    }

    .week-interval-list.compact .interval-extra {
      display: none;
    }

    .week-interval-list .interval-more {
      display: none;
      cursor: pointer;
    }

    .week-interval-list.compact .interval-more {
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    .week-interval-list.compact .interval-primary {
      min-width: 0;
      max-width: calc(100% - 38px);
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .interval-more:hover {
      transform: none;
    }

    .interval-popover {
      width: min(280px, calc(100vw - 24px));
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-strong);
      color: var(--text);
      box-shadow: var(--shadow);
      padding: 10px;
    }

    .interval-popover::backdrop {
      background: rgba(34, 39, 36, 0.18);
    }

    .interval-popover .section-title {
      margin-bottom: 8px;
    }

    .interval-popover-list {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }

    .event-row {
      margin: 0;
    }

    .event-chip {
      border-radius: 999px;
      padding: 5px 8px;
      color: var(--gold);
      background: rgba(212, 191, 136, 0.09);
      border: 1px solid rgba(212, 191, 136, 0.24);
      font-size: 0.72rem;
    }

    textarea {
      width: 100%;
      resize: vertical;
      min-height: 72px;
      border: 1px solid rgba(221, 222, 204, 0.16);
      border-radius: 14px;
      background: rgba(9, 12, 8, 0.52);
      color: var(--text);
      padding: 11px 12px;
      line-height: 1.4;
    }

    textarea:focus, select:focus {
      outline: 2px solid rgba(168, 199, 160, 0.28);
      border-color: var(--line-strong);
    }

    .status-text {
      min-height: 16px;
      color: var(--accent);
      font-size: 0.72rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .top-status {
      max-width: min(34vw, 420px);
      color: rgba(238, 234, 222, 0.62);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.62rem;
      letter-spacing: 0.05em;
    }

    .ride-sidebar {
      position: fixed;
      top: 118px;
      right: 18px;
      z-index: 26;
      display: none;
      width: min(430px, calc(100vw - 36px));
      max-height: calc(100vh - 142px);
      overflow: auto;
      border: 1px solid rgba(168, 199, 160, 0.22);
      border-radius: 24px;
      background: rgba(15, 19, 14, 0.96);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      padding: 15px;
    }

    .ride-sidebar.open {
      display: grid;
      gap: 14px;
    }

    .ride-sidebar h3,
    .ride-sidebar h4 {
      margin: 0;
    }

    .ride-sidebar h3 {
      font-size: 1rem;
      letter-spacing: -0.02em;
    }

    .ride-sidebar h4 {
      color: var(--accent);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.64rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }

    .ride-sidebar-content,
    .sidebar-section,
    .activity-list,
    .activity-card {
      display: grid;
      gap: 10px;
      min-width: 0;
    }

    .sidebar-section {
      border: 1px solid rgba(238, 234, 222, 0.08);
      border-radius: 18px;
      background: rgba(238, 234, 222, 0.035);
      padding: 12px;
    }

    .plan-execution-block {
      display: grid;
      gap: 8px;
    }

    .stat-list,
    .lap-list {
      display: grid;
      gap: 6px;
      min-width: 0;
    }

    .stat-row,
    .lap-row {
      display: grid;
      gap: 10px;
      align-items: baseline;
      border-bottom: 1px solid rgba(238, 234, 222, 0.06);
      padding: 6px 0;
    }

    .stat-row {
      grid-template-columns: minmax(120px, 0.85fr) minmax(0, 1.15fr);
    }

    .lap-row {
      grid-template-columns: minmax(52px, 0.55fr) repeat(4, minmax(0, 1fr));
    }

    .stat-row:last-child,
    .lap-row:last-child {
      border-bottom: 0;
    }

    .stat-row span,
    .lap-row span {
      min-width: 0;
      color: rgba(238, 234, 222, 0.58);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.58rem;
      letter-spacing: 0.08em;
      line-height: 1.3;
      overflow: hidden;
      text-overflow: ellipsis;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .stat-row strong,
    .lap-row strong {
      min-width: 0;
      color: rgba(238, 234, 222, 0.86);
      font-size: 0.78rem;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }

    .lap-row.header span {
      color: var(--accent);
      font-size: 0.54rem;
    }

    .activity-meta,
    .sidebar-copy {
      color: rgba(238, 234, 222, 0.68);
      font-size: 0.8rem;
      line-height: 1.35;
    }

    .activity-card {
      border: 1px solid rgba(168, 199, 160, 0.12);
      border-radius: 15px;
      background: rgba(16, 19, 15, 0.5);
      padding: 11px;
    }

    .activity-card a {
      color: var(--text);
      font-weight: 700;
      text-decoration-color: rgba(168, 199, 160, 0.42);
      text-underline-offset: 3px;
    }

    .activity-card a:hover {
      color: var(--accent);
    }

    .coach-note-list {
      display: grid;
      gap: 10px;
      min-width: 0;
    }

    .coach-note-card {
      display: grid;
      gap: 7px;
      min-width: 0;
      border: 1px solid rgba(215, 207, 170, 0.16);
      border-radius: 15px;
      background: rgba(215, 207, 170, 0.055);
      padding: 11px;
    }

    .coach-note-card-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }

    .coach-note-card strong {
      min-width: 0;
      line-height: 1.25;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .coach-note-card a {
      color: var(--gold);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.62rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      text-decoration-color: rgba(212, 191, 136, 0.46);
      text-underline-offset: 3px;
      white-space: nowrap;
    }

    .coach-note-card .sidebar-copy,
    .coach-note-card .activity-meta span {
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .coach-note-card .activity-meta span {
      white-space: normal;
    }

    .activity-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 10px;
    }

    .activity-meta span {
      white-space: nowrap;
    }

    .stat-section-title {
      color: rgba(238, 234, 222, 0.52);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.56rem;
      letter-spacing: 0.09em;
      margin: 2px 0 -2px;
      text-transform: uppercase;
    }

    @media (max-width: 1320px) {
      .topbar {
        grid-template-columns: minmax(180px, auto) minmax(320px, 1fr) auto;
      }

      .toolbar {
        align-items: flex-start;
        flex-direction: column;
      }

      .toolbar-actions {
        justify-content: flex-start;
        width: 100%;
      }

      .context-stack {
        top: 190px;
        max-height: calc(100vh - 214px);
      }
    }

    @media (max-width: 1180px) {
      .summary-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .month-weekdays, .month-week-row {
        grid-template-columns: repeat(7, minmax(0, 1fr));
      }

      .week-total-heading, .calendar-week-stat {
        display: none;
      }
    }

    @media (max-width: 880px) {
      body {
        scroll-padding-top: 220px;
      }

      .page {
        width: min(100vw - 20px, 1580px);
        padding-top: 14px;
      }

      .topbar, .workspace, .week-head {
        grid-template-columns: 1fr;
      }

      .tabs, .topbar-actions {
        grid-column: auto;
      }

      .tabs {
        flex-wrap: wrap;
      }

      .topbar-actions {
        justify-content: flex-start;
      }

      .top-status {
        flex-basis: 100%;
        max-width: 100%;
      }

      .calendar-grid {
        grid-template-columns: 1fr;
      }

      .month-name-row {
        align-items: flex-start;
        flex-direction: column;
      }

      .signal-key {
        justify-content: flex-start;
      }

      .month-summary-card {
        align-items: flex-start;
        flex-direction: column;
      }

      .month-summary-main,
      .month-summary-line {
        min-width: 0;
      }

      .month-summary-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
        width: 100%;
      }

      .week-days {
        grid-template-columns: 1fr;
      }

      .today-dashboard,
      body[data-view="today"] .workspace {
        grid-template-columns: 1fr;
      }

      .today-grid {
        grid-template-columns: 1fr;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .ride-stat-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .stat-row {
        grid-template-columns: minmax(92px, 0.8fr) minmax(0, 1.2fr);
      }

      .lap-row {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .lap-row.header {
        display: none;
      }

      .toolbar {
        align-items: flex-start;
        flex-direction: column;
      }

      .ride-sidebar {
        inset: auto 10px 10px 10px;
        width: auto;
        max-height: 72vh;
        z-index: 45;
      }
    }

    /* Coaching workspace layout */
    :root {
      --bg: #f7f8f5;
      --panel: rgba(255, 255, 255, 0.9);
      --panel-strong: #ffffff;
      --line: #e4e8e0;
      --line-strong: #b9d7b4;
      --text: #222724;
      --muted: #70776f;
      --accent: #5aa34f;
      --accent-soft: #eef8ec;
      --gold: #d69d21;
      --warning: #d66852;
      --blue: #4a86d9;
      --shadow: 0 12px 34px rgba(33, 41, 34, 0.08);
      --shadow-soft: 0 8px 20px rgba(33, 41, 34, 0.06);
      --surface: rgba(255, 255, 255, 0.78);
      --surface-muted: #f1f4ef;
    }

    body {
      color: var(--text);
      overflow-x: hidden;
      background:
        linear-gradient(rgba(247, 248, 245, 0.94), rgba(247, 248, 245, 0.94)),
        repeating-linear-gradient(17deg, rgba(63, 82, 60, 0.055) 0 1px, transparent 1px 42px),
        repeating-linear-gradient(107deg, rgba(63, 82, 60, 0.04) 0 1px, transparent 1px 54px),
        #f7f8f5;
    }

    button,
    .ghost-link {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      color: var(--text);
      box-shadow: 0 1px 1px rgba(35, 42, 35, 0.02);
    }

    button:hover,
    .ghost-link:hover {
      border-color: #cbd8c7;
      background: #f9fbf8;
      transform: translateY(-1px);
    }

    button.active {
      border-color: transparent;
      color: #285f28;
      background: #f4faf2;
    }

    .page {
      width: min(1536px, calc(100vw - 14px));
      padding: 10px 0 24px;
    }

    .topbar {
      grid-template-columns: minmax(250px, 0.95fr) minmax(280px, 1fr) minmax(260px, 0.95fr);
      gap: 12px;
      margin-bottom: 12px;
      padding: 8px 10px;
      border-radius: 8px;
      border-color: rgba(36, 42, 37, 0.08);
      background: rgba(255, 255, 255, 0.86);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(18px);
    }

    .brand {
      display: grid;
      align-items: start;
      gap: 3px;
    }

    .brand-mark {
      color: #c6242f;
      font-size: 1.55rem;
      font-weight: 800;
      letter-spacing: 0.02em;
      line-height: 1;
      transform: skew(-8deg);
    }

    .brand-kicker {
      margin: 0;
      color: #151916;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.2em;
      text-transform: uppercase;
    }

    .tabs {
      justify-content: center;
      gap: 0;
      align-self: stretch;
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 0 2px;
    }

    .tab {
      position: relative;
      min-width: 104px;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      color: #5d645d;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.86rem;
      font-weight: 500;
      letter-spacing: 0;
      text-transform: none;
    }

    .tab.active {
      color: #111611;
      background: transparent;
    }

    .tab.active::after {
      content: "";
      position: absolute;
      left: 16px;
      right: 16px;
      bottom: -1px;
      height: 2px;
      border-radius: 999px;
      background: var(--accent);
    }

    .topbar-actions {
      gap: 7px;
    }

    .topbar-actions button {
      min-height: 40px;
      padding: 8px 12px;
      color: #333a34;
      font-size: 0.78rem;
    }

    .action-menu {
      border-color: var(--line);
      background: #fff;
      box-shadow: var(--shadow-soft);
    }

    .topbar-actions .action-menu button {
      min-height: 36px;
      justify-content: flex-start;
      padding: 8px 10px;
      border: 0;
      color: var(--text);
      background: transparent;
      box-shadow: none;
      white-space: nowrap;
    }

    .topbar-actions .action-menu button:hover,
    .topbar-actions .action-menu button:focus-visible {
      border-color: transparent;
      background: #f4faf2;
      transform: none;
    }

    .topbar-actions .action-menu .action-menu-status {
      margin: 2px 10px 7px;
      color: #67736b;
      font-size: 0.64rem;
      line-height: 1.35;
    }

    .sync-button.sync-running {
      color: #9a6400;
      border-color: #e6c77c;
      background: #fff7e3;
    }

    .top-status {
      max-width: 220px;
      color: var(--muted);
      letter-spacing: 0;
      text-transform: none;
    }

    .workspace {
      grid-template-columns: minmax(268px, 330px) minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }

    .context-stack {
      position: sticky;
      top: 72px;
      align-self: start;
      display: grid;
      gap: 12px;
      max-height: calc(100vh - 86px);
      overflow: auto;
    }

    .center-stage {
      display: grid;
      gap: 12px;
      min-width: 0;
    }

    .topbar,
    .workspace,
    .context-stack,
    .coach-rail,
    .center-stage,
    .ride-sidebar,
    .panel,
    .tabs,
    .topbar-actions {
      min-width: 0;
    }

    .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(10px);
    }

    .coach-rail {
      display: grid;
      gap: 14px;
      padding: 14px;
    }

    .coach-rail-head,
    .section-title-row,
    .month-rail-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }

    .coach-rail h2,
    .month-rail h2 {
      margin: 0;
      font-size: 1.38rem;
      letter-spacing: -0.03em;
      line-height: 1.1;
    }

    .day-nav {
      display: flex;
      gap: 6px;
    }

    .day-nav .icon-button {
      width: 32px;
      min-width: 32px;
      height: 32px;
      font-size: 0.85rem;
    }

    .coach-rail-content,
    .rail-section {
      display: grid;
      gap: 12px;
      min-width: 0;
    }

    .rail-section {
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }

    .rail-section:last-child {
      border-bottom: 0;
      padding-bottom: 0;
    }

    .section-title,
    .rail-note-label span {
      margin: 0;
      color: #1e2520;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.67rem;
      font-weight: 700;
      letter-spacing: 0.11em;
      text-transform: uppercase;
    }

    .section-title span {
      color: var(--muted);
      font-weight: 500;
      letter-spacing: 0.04em;
    }

    .phase-chip,
    .complete-dot,
    .quiet-dot {
      width: fit-content;
      border-radius: 999px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.58rem;
      letter-spacing: 0.05em;
      line-height: 1;
      padding: 6px 8px;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .phase-chip {
      color: #2e6c2f;
      background: #edf7ea;
      border: 1px solid #d7ead1;
    }

    .complete-dot {
      color: #2e6c2f;
      background: #edf7ea;
    }

    .quiet-dot {
      color: #7b6550;
      background: #fbf3e4;
    }

    .session-card,
    .actual-card {
      display: grid;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }

    .session-card {
      grid-template-columns: 30px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      padding: 12px;
    }

    .session-icon {
      display: grid;
      width: 28px;
      height: 28px;
      place-items: center;
      border-radius: 999px;
      color: #347a32;
      background: #eff8ec;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-weight: 700;
    }

    .session-card strong,
    .actual-card strong {
      display: block;
      color: #202620;
      font-size: 0.92rem;
      line-height: 1.25;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .session-card p,
    .actual-card p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 0.76rem;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .session-card > span:last-child {
      color: #3f473f;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.72rem;
      white-space: nowrap;
    }

    .actual-card {
      gap: 8px;
      padding: 12px;
      border-left: 3px solid #f05b2a;
    }

    .text-link {
      width: fit-content;
      color: #326dc2;
      font-size: 0.74rem;
      text-decoration-color: rgba(50, 109, 194, 0.35);
      text-underline-offset: 3px;
    }

    .rail-mini-grid,
    .recovery-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }

    .rail-mini-grid div,
    .recovery-list span {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfa;
      padding: 9px;
    }

    .rail-mini-grid strong,
    .recovery-list strong {
      display: block;
      color: #1f251f;
      font-size: 1rem;
      line-height: 1;
    }

    .rail-mini-grid span,
    .recovery-list span {
      color: var(--muted);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.56rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .recovery-card {
      grid-template-columns: minmax(96px, 0.8fr) minmax(0, 1.2fr);
      align-items: center;
    }

    .recovery-score {
      --score: 62;
      display: flex;
      flex-direction: column;
      width: 116px;
      height: 116px;
      margin-top: 10px;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      background:
        radial-gradient(circle at center, #fff 0 55%, transparent 56%),
        conic-gradient(var(--blue) calc(var(--score) * 1%), #e8edf4 0);
      border: 1px solid #e1e7ef;
    }

    .recovery-score strong {
      color: var(--blue);
      font-size: 2.2rem;
      letter-spacing: -0.07em;
      line-height: 0.9;
    }

    .recovery-score span {
      color: #326dc2;
      font-size: 0.68rem;
      margin-top: 4px;
    }

    .recovery-list {
      grid-template-columns: 1fr;
    }

    .rail-note-label {
      display: grid;
      gap: 8px;
    }

    textarea {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      color: var(--text);
    }

    textarea:focus,
    select:focus,
    input[type="date"]:focus {
      outline: 2px solid rgba(74, 134, 217, 0.18);
      border-color: #b7cdf0;
    }

    .toolbar {
      min-height: 56px;
      padding: 13px 15px;
      border-bottom-color: var(--line);
      background: rgba(255, 255, 255, 0.58);
    }

    .toolbar h2 {
      color: #1e251f;
      font-size: 0.72rem;
      letter-spacing: 0.13em;
    }

    .toolbar .meta {
      color: var(--muted);
    }

    .toolbar select,
    .toolbar input[type="date"] {
      border-color: var(--line);
      border-radius: 7px;
      background: var(--surface-muted);
      color: #232a23;
    }

    #weeks-view {
      overflow: hidden;
      background: rgba(255, 255, 255, 0.82);
    }

    .week-list {
      padding: 0;
      gap: 0;
    }

    .week-card {
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 14px;
    }

    .week-head {
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      margin-bottom: 12px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line);
    }

    .week-title {
      margin-bottom: 5px;
      color: #1f251f;
      font-size: 1.15rem;
      letter-spacing: -0.03em;
    }

    .week-focus {
      max-width: 820px;
      color: #576057;
      font-size: 0.86rem;
    }

    .pill {
      border-color: #d7ead1;
      color: #2e6c2f;
      background: #edf7ea;
      font-size: 0.58rem;
      border-radius: 999px;
    }

    .pill.warn {
      border-color: #ead9ad;
      color: #875e12;
      background: #fbf3dc;
    }

    .week-overview {
      gap: 12px;
      margin-bottom: 14px;
    }

    .summary-grid {
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
    }

    .summary-card {
      min-height: 70px;
      border-color: var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 11px;
    }

    .summary-value {
      color: #1f251f;
      font-size: 1.22rem;
      letter-spacing: -0.04em;
      margin-bottom: 8px;
    }

    .stat-label {
      color: var(--muted);
      font-size: 0.54rem;
      letter-spacing: 0.08em;
    }

    .progress-label {
      color: #687168;
      font-size: 0.6rem;
      letter-spacing: 0.09em;
    }

    .progress-track {
      height: 10px;
      border-color: #dfe5db;
      background: #edf1eb;
    }

    .progress-fill {
      background: linear-gradient(90deg, #73b968, #d7ae35);
    }

    .progress-min {
      background: #b17b0c;
    }

    .plan-detail {
      display: none;
    }

    .week-detail {
      color: #5a625a;
      font-size: 0.82rem;
    }

    .week-days {
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 8px;
      align-items: stretch;
    }

    .week-day {
      min-height: 304px;
      height: 304px;
      border-color: var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      gap: 8px;
      box-shadow: none;
    }

    .week-day.selected {
      border-color: #87c47d;
      box-shadow: inset 0 0 0 1px #87c47d;
    }

    .week-day.has-ride {
      box-shadow: inset 0 -3px 0 rgba(90, 163, 79, 0.52);
    }

    .week-day.selected.has-ride {
      box-shadow: inset 0 0 0 1px #87c47d, inset 0 -3px 0 rgba(90, 163, 79, 0.62);
    }

    .week-day.hard-day {
      background: #fffaf0;
    }

    .date-label {
      color: #4b534c;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.64rem;
      font-weight: 700;
      letter-spacing: 0.07em;
      text-transform: uppercase;
    }

    .planned,
    .actual,
    .metric-line,
    .empty-ride-stats {
      color: #626b62;
      font-size: 0.74rem;
      line-height: 1.32;
    }

    .planned {
      color: #283028;
      font-size: 0.8rem;
      font-weight: 700;
    }

    .week-day .planned,
    .week-day .actual {
      display: block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .week-day .actual {
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      color: #283028;
      font-weight: 700;
      white-space: normal;
    }

    .week-day-title-stack .planned {
      color: #626b62;
      font-size: 0.72rem;
      font-weight: 500;
    }

    .actual-link {
      color: #326dc2;
      text-decoration-color: rgba(50, 109, 194, 0.35);
    }

    .week-stat-chip-grid,
    .ride-stat-grid {
      gap: 5px;
    }

    .week-stat-chip,
    .ride-stat-chip {
      border-color: #e3eadf;
      border-radius: 7px;
      background: #fbfcfa;
    }

    .week-stat-chip strong,
    .ride-stat-chip strong {
      color: #242b25;
      font-size: 0.72rem;
    }

    .week-stat-chip span,
    .ride-stat-chip span {
      color: #727a72;
      font-size: 0.49rem;
      letter-spacing: 0.06em;
    }

    .week-day .week-stat-chip-grid .week-stat-chip:nth-child(n+3) {
      display: none;
    }

    .week-day .week-stat-chip {
      padding: 6px;
    }

    .week-day .week-stat-chip strong {
      overflow: visible;
      font-size: 0.68rem;
      text-overflow: clip;
      white-space: normal;
    }

    .week-day .week-stat-chip span {
      display: none;
    }

    .day-spark {
      display: grid;
      align-items: end;
      position: relative;
      width: min(100%, var(--spark-max-width, 100%));
      height: 41px;
      margin: 2px 0 0;
      overflow: hidden;
    }

    .day-spark svg {
      display: block;
      width: 100%;
      height: 100%;
      overflow: hidden;
    }

    .week-load-chart svg,
    .today-load-card svg {
      display: block;
      width: 100%;
      height: 100%;
      overflow: visible;
    }

    .day-spark .spark-segment {
      stroke-linecap: round;
      stroke-width: 2.6;
    }

    .day-spark .spark-fill {
      fill: rgba(90, 163, 79, 0.09);
    }

    .day-spark .spark-elevation-fill {
      fill: rgba(112, 119, 111, 0.13);
    }

    .day-spark .spark-elevation-line {
      fill: none;
      stroke: rgba(112, 119, 111, 0.28);
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 1.2;
    }

    .day-spark .zone-z1 { --zone-color: #3d8bd9; }
    .day-spark .zone-z2 { --zone-color: #57a85b; }
    .day-spark .zone-z3 { --zone-color: #d2ad39; }
    .day-spark .zone-z4 { --zone-color: #dc8437; }
    .day-spark .zone-z5 { --zone-color: #d55b55; }
    .day-spark .zone-z6 { --zone-color: #9a6bd9; }
    .day-spark .zone-hr { --zone-color: #5aa34f; }

    .day-spark .spark-segment.zone-z1,
    .day-spark .spark-segment.zone-z2,
    .day-spark .spark-segment.zone-z3,
    .day-spark .spark-segment.zone-z4,
    .day-spark .spark-segment.zone-z5,
    .day-spark .spark-segment.zone-z6,
    .day-spark .spark-segment.zone-hr {
      stroke: var(--zone-color);
    }

    .day-spark .spark-tag {
      position: absolute;
      top: -2px;
      right: 0;
      z-index: 1;
      color: #71806f;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.46rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      line-height: 1;
      text-transform: uppercase;
    }

    .day-spark.bars {
      display: grid;
      height: 39px;
      padding: 8px 3px 0;
    }

    .day-spark .spark-bars {
      display: flex;
      align-items: end;
      gap: 3px;
      height: 100%;
      min-width: 0;
    }

    .day-spark.bars i {
      flex: var(--w, 1) 1 0;
      height: var(--h, 35%);
      min-height: 5px;
      border-radius: 2px 2px 0 0;
      background: #e3b536;
    }

    .day-spark.bars.actual i {
      background: linear-gradient(180deg, #4f9646, #93bd8a);
    }

    .day-spark.bars.actual i.zone-z1,
    .day-spark.bars.actual i.zone-z2,
    .day-spark.bars.actual i.zone-z3,
    .day-spark.bars.actual i.zone-z4,
    .day-spark.bars.actual i.zone-z5,
    .day-spark.bars.actual i.zone-z6,
    .day-spark.bars.actual i.zone-hr {
      background: linear-gradient(180deg, color-mix(in srgb, var(--zone-color) 88%, white), var(--zone-color));
    }

    .day-spark.bars.planned i.zone-z1,
    .day-spark.bars.planned i.zone-z2,
    .day-spark.bars.planned i.zone-z3,
    .day-spark.bars.planned i.zone-z4,
    .day-spark.bars.planned i.zone-z5,
    .day-spark.bars.planned i.zone-z6,
    .day-spark.bars.planned i.zone-hr {
      background: linear-gradient(
        180deg,
        color-mix(in srgb, var(--zone-color) 24%, white),
        color-mix(in srgb, var(--zone-color) 48%, white)
      );
    }

    .day-spark.empty {
      height: 39px;
      border-bottom: 1px dashed #d8ded5;
    }

    .week-load-chart,
    .today-load-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(248, 250, 247, 0.94));
      padding: 12px;
    }

    .week-load-chart {
      display: grid;
      gap: 10px;
    }

    .week-load-chart-head,
    .chart-legend {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .week-load-chart h4,
    .today-card h4 {
      margin: 0;
      color: #242b25;
      font-size: 0.82rem;
      letter-spacing: 0.01em;
    }

    .chart-legend {
      justify-content: flex-end;
      color: #687168;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.56rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .chart-legend i {
      display: inline-block;
      width: 16px;
      height: 2px;
      margin-right: 4px;
      vertical-align: middle;
      background: #5aa34f;
    }

    .chart-legend .planned i {
      background: #9fa79f;
    }

    .week-load-chart .grid-line,
    .today-load-card .grid-line {
      stroke: #e6ebe2;
      stroke-width: 1;
    }

    .week-load-chart .planned-line,
    .today-load-card .planned-line {
      fill: none;
      stroke: #9fa79f;
      stroke-dasharray: 5 5;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 2;
    }

    .week-load-chart .actual-line,
    .today-load-card .actual-line {
      fill: none;
      stroke: #5aa34f;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 3;
    }

    .week-load-chart .actual-area,
    .today-load-card .actual-area {
      fill: rgba(90, 163, 79, 0.08);
    }

    .week-load-chart .chart-dot,
    .today-load-card .chart-dot {
      fill: #5aa34f;
      stroke: #fff;
      stroke-width: 2;
    }

    .interval-chip,
    .event-chip {
      border-radius: 999px;
      font-size: 0.5rem;
    }

    .interval-chip {
      color: #8d660c;
      border-color: #ecd79d;
      background: #fff8e8;
    }

    .event-chip {
      color: #8d660c;
      border-color: #ecd79d;
      background: #fff8e8;
    }

    .month-rail {
      display: grid;
      gap: 12px;
      padding: 13px 15px;
    }

    .race-marker-list {
      display: flex;
      gap: 8px;
      align-items: stretch;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .race-marker {
      display: grid;
      min-width: 148px;
      gap: 3px;
      padding: 9px 10px;
      text-align: left;
      border-left: 3px solid #f05b2a;
    }

    .race-marker span,
    .race-marker small,
    .quiet-context {
      color: var(--muted);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.55rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .race-marker strong {
      color: #252b25;
      font-size: 0.74rem;
      line-height: 1.15;
    }

    .month-strip-days {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(25px, 1fr));
      gap: 6px;
      align-items: end;
    }

    .month-strip-day {
      display: grid;
      min-width: 0;
      height: 42px;
      gap: 4px;
      place-items: center;
      padding: 4px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      box-shadow: none;
    }

    .month-strip-day span {
      color: #676f67;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.58rem;
    }

    .month-strip-day i {
      display: block;
      width: 100%;
      height: 6px;
      border-radius: 999px;
      background: #d3d8d0;
    }

    .month-strip-day.has-ride i {
      background: #7abd6f;
    }

    .month-strip-day.interval i {
      background: #e3b536;
    }

    .month-strip-day.race i {
      background: #f05b2a;
    }

    .month-strip-day.selected {
      background: #e9f5e6;
    }

    .month-strip-day.selected span {
      color: #2d742d;
      font-weight: 700;
    }

    .month-strip-day.today {
      outline: 1px solid #87c47d;
    }

    .today-dashboard {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 12px;
      padding: 14px;
    }

    .today-card {
      display: grid;
      gap: 12px;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
    }

    .today-card.primary {
      grid-template-rows: auto auto auto auto;
      align-content: start;
    }

    .today-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }

    .today-card h3 {
      margin: 0;
      color: #1f251f;
      font-size: 1.7rem;
      line-height: 1;
      letter-spacing: -0.04em;
    }

    .today-plan-copy {
      margin: 0;
      max-width: 680px;
      color: #535d54;
      font-size: 1rem;
      line-height: 1.45;
    }

    .today-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .today-grid .summary-card {
      min-height: 84px;
    }

    .today-load-card {
      min-height: 184px;
    }

    .today-note-card textarea {
      min-height: 132px;
    }

    .today-context-stack {
      display: grid;
      gap: 18px;
      align-content: start;
    }

    .today-context-block {
      display: grid;
      gap: 7px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--rule);
    }

    .today-context-block:last-child {
      border-bottom: 0;
      padding-bottom: 0;
    }

    .today-context-block h4 {
      margin: 0;
      color: var(--ink);
      font-size: 1.32rem;
      line-height: 1.05;
      letter-spacing: -0.035em;
    }

    .today-context-block p {
      margin: 0;
      max-width: 36rem;
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.52;
    }

    .today-context-number {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px;
      height: 22px;
      border: 1px solid rgba(80, 108, 78, 0.28);
      border-radius: 999px;
      color: var(--accent);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.52rem;
      letter-spacing: 0.06em;
    }

    .ride-sidebar {
      position: static;
      z-index: auto;
      display: none;
      width: auto;
      max-height: none;
      overflow: visible;
      border-color: var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: var(--shadow-soft);
      padding: 14px;
      backdrop-filter: blur(12px);
    }

    .ride-sidebar.open {
      display: grid;
    }

    .ride-sidebar h3 {
      color: #1f251f;
    }

    .ride-sidebar h4 {
      color: #4c8c45;
      font-size: 0.58rem;
    }

    .sidebar-section {
      border-color: var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 11px;
    }

    .activity-card,
    .coach-note-card {
      border-radius: 8px;
      background: #fbfcfa;
    }

    .activity-card {
      border-color: #e3eadf;
    }

    .coach-note-card {
      border-color: #edd998;
      background: #fffaf0;
    }

    .activity-card a {
      color: #224d91;
      text-decoration-color: rgba(34, 77, 145, 0.32);
    }

    .activity-meta,
    .sidebar-copy {
      color: #626b62;
    }

    .stat-row,
    .lap-row {
      border-bottom-color: #edf1eb;
    }

    .stat-row span,
    .lap-row span,
    .stat-section-title {
      color: #767e76;
    }

    .stat-row strong,
    .lap-row strong {
      color: #252c26;
    }

    .calendar-grid {
      background: transparent;
    }

    .month-card {
      border-radius: 8px;
      background: #fff;
    }

    .calendar-day,
    .blank-day,
    .calendar-week-stat {
      border-radius: 8px;
      background: #fbfcfa;
    }

    .month-name-row {
      border-bottom-color: var(--line);
    }

    .month-name {
      color: #1f251f;
    }

    .weekday,
    .week-total-heading {
      color: #7b837b;
    }

    .calendar-day {
      border-color: #e1e7dd;
      color: #242b25;
      background: #fff;
      box-shadow: none;
    }

    .blank-day {
      background: #f2f5f0;
    }

    .calendar-day.today {
      border-color: #87c47d;
      background: #f7fcf5;
    }

    .calendar-day.selected {
      border-color: #5aa34f;
      background: #eef8ec;
      outline: 1px solid #5aa34f;
    }

    .calendar-day.has-ride {
      box-shadow: inset 0 -3px 0 rgba(90, 163, 79, 0.48);
    }

    .calendar-day.has-event {
      box-shadow: inset 0 3px 0 rgba(214, 157, 33, 0.42);
    }

    .calendar-day.interval-day {
      border-color: #ead48d;
      background: #fffaf0;
    }

    .calendar-day.interval-day::before {
      opacity: 1;
      background: #d6a321;
    }

    .calendar-day.race-day,
    .calendar-day.race-road-day,
    .calendar-day.race-dirt-day {
      border-color: #f1b49f;
      background: #fff5f1;
      box-shadow: inset 0 0 0 1px rgba(214, 104, 82, 0.08);
    }

    .calendar-day.race-road-day {
      border-color: #a9c6ef;
      background: #f2f7ff;
    }

    .calendar-day.race-dirt-day {
      border-color: #f0b39d;
      background: #fff5ef;
    }

    .calendar-day.big-day:not(.race-day) {
      border-color: #ead48d;
      background: #fffaf0;
      box-shadow: inset 0 -3px 0 rgba(214, 157, 33, 0.45);
    }

    .calendar-day.interval-day.big-day:not(.race-day) {
      border-color: #ead48d;
      background: #fffaf0;
      box-shadow: inset 0 -3px 0 rgba(214, 157, 33, 0.45);
    }

    .calendar-day.race-day.big-day,
    .calendar-day.race-day.has-event,
    .calendar-day.race-day.has-ride {
      background: #fff5f1;
      box-shadow: inset 0 -3px 0 rgba(214, 104, 82, 0.38);
    }

    .calendar-day.selected.interval-day,
    .calendar-day.selected.big-day,
    .calendar-day.selected.has-ride,
    .calendar-day.selected.has-event {
      border-color: #5aa34f;
      background: #eef8ec;
      box-shadow: inset 0 0 0 1px #5aa34f;
    }

    .day-mini {
      color: #555f56;
    }

    .day-kpi {
      color: #7a837a;
    }

    .day-tag {
      color: #687168;
      border-color: #dde5da;
      background: #f7f9f5;
    }

    .day-tag.interval {
      color: #8d660c;
      border-color: #eddca6;
      background: #fff8e8;
    }

    .day-tag.big {
      color: #8d660c;
      border-color: #eddca6;
      background: #fff8e8;
    }

    .day-tag.race-road {
      color: #326dc2;
      border-color: #c6daf6;
      background: #f2f7ff;
    }

    .day-tag.race-dirt {
      color: #ad4c36;
      border-color: #f0c2b2;
      background: #fff5ef;
    }

    .day-tag.coach-note {
      color: #7a5c12;
      border-color: #ead9a9;
      background: #fff9e9;
    }

    .week-day-signal {
      background: #dfe5dc;
    }

    .week-day-signal.interval {
      background: #d69d21;
    }

    .week-day-signal.big {
      background: #d69d21;
    }

    .week-day-signal.race-road {
      background: #4a86d9;
    }

    .week-day-signal.race-dirt {
      background: #d66852;
    }

    .week-day-signal.coach-note {
      background: #d69d21;
    }

    .week-day-signals.expanded .week-day-signal {
      border-color: #dde5da;
      color: #687168;
      background: #f7f9f5;
    }

    .week-day-signals.expanded .week-day-signal.interval {
      color: #8d660c;
      border-color: #eddca6;
      background: #fff8e8;
    }

    .week-day-signals.expanded .week-day-signal.big {
      color: #8d660c;
      border-color: #eddca6;
      background: #fff8e8;
    }

    .week-day-signals.expanded .week-day-signal.race-road {
      color: #326dc2;
      border-color: #c6daf6;
      background: #f2f7ff;
    }

    .week-day-signals.expanded .week-day-signal.race-dirt {
      color: #ad4c36;
      border-color: #f0c2b2;
      background: #fff5ef;
    }

    .week-day-signals.expanded .week-day-signal.coach-note {
      color: #7a5c12;
      border-color: #ead9a9;
      background: #fff9e9;
    }

    .calendar-week-stat {
      border-color: #dce6d8;
      background: #f8fbf6;
    }

    .calendar-week-stat strong {
      color: #252c26;
    }

    .calendar-week-stat span {
      color: #687168;
    }

    .calendar-week-stat .stat-label {
      color: #4b9142;
    }

    .month-summary-card {
      border-color: #dce6d8;
      background: #f8fbf6;
    }

    .month-summary-main strong,
    .month-summary-stat strong,
    .month-summary-line strong {
      color: #252c26;
    }

    .month-summary-main span,
    .month-summary-line span,
    .month-summary-stat span {
      color: #687168;
    }

    body[data-view="today"] .context-stack,
    body[data-view="today"] .coach-rail,
    body[data-view="today"] .ride-sidebar {
      display: none;
    }

    body[data-view="today"] .workspace {
      grid-template-columns: minmax(0, 1fr);
    }

    body[data-view="connections"] .context-stack,
    body[data-view="connections"] .coach-rail,
    body[data-view="connections"] .ride-sidebar,
    body[data-view="connections"] .month-rail,
    body[data-view="settings"] .context-stack,
    body[data-view="settings"] .coach-rail,
    body[data-view="settings"] .ride-sidebar,
    body[data-view="settings"] .month-rail {
      display: none;
    }

    body[data-view="connections"] .workspace,
    body[data-view="settings"] .workspace {
      grid-template-columns: minmax(0, 1fr);
    }

    body[data-view="calendar"] .month-rail {
      display: none;
    }

    @media (max-width: 1320px) {
      .topbar {
        grid-template-columns: minmax(180px, auto) minmax(320px, 1fr) auto;
      }

      .workspace {
        grid-template-columns: minmax(250px, 310px) minmax(0, 1fr);
      }

      .context-stack {
        max-height: calc(100vh - 86px);
      }

      .summary-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .week-days {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
    }

    @media (max-width: 960px) {
      .page {
        width: calc(100vw - 20px);
        max-width: calc(100vw - 20px);
        overflow: hidden;
      }

      .workspace,
      .week-head,
      .recovery-card {
        grid-template-columns: 1fr;
      }

      .topbar {
        grid-template-columns: minmax(0, 1fr) auto;
        grid-template-areas:
          "brand actions"
          "tabs tabs";
        align-items: center;
      }

      .brand {
        grid-area: brand;
      }

      .tabs {
        grid-area: tabs;
        justify-content: stretch;
        width: 100%;
        overflow: hidden;
      }

      .topbar-actions {
        grid-area: actions;
        width: auto;
        justify-content: flex-end;
      }

      .tab {
        min-width: 0;
        flex: 1;
        padding-inline: 4px;
      }

      .coach-rail,
      .ride-sidebar,
      .context-stack {
        position: static;
        width: 100%;
        max-width: 100%;
        max-height: none;
        overflow: hidden;
      }

      .center-stage {
        order: 1;
      }

      .context-stack {
        order: 2;
      }

      body[data-view="weeks"] .coach-rail {
        display: none;
      }

      .section-title-row {
        flex-wrap: wrap;
      }

      .phase-chip {
        max-width: 100%;
        overflow-wrap: anywhere;
        white-space: normal;
      }

      .session-card,
      .actual-card,
      .rail-section,
      .rail-note-label {
        max-width: 100%;
      }

      .actual-card strong,
      .actual-card p {
        max-width: 100%;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .week-days {
        grid-template-columns: 1fr;
      }

      .today-dashboard {
        grid-template-columns: 1fr;
      }

      .today-grid {
        grid-template-columns: 1fr;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .month-rail-head {
        display: grid;
      }

      .race-marker-list {
        justify-content: stretch;
      }

      .month-summary-grid {
        flex: 0 0 auto;
      }

      .toolbar {
        gap: 10px;
        padding: 12px;
      }

      .toolbar > div,
      .toolbar-actions {
        width: 100%;
      }

      .today-toolbar-actions input[type="date"] {
        width: 100%;
      }
    }

    @media (max-width: 640px) {
      .topbar {
        gap: 10px;
        padding: 10px;
      }

      .brand-mark {
        font-size: 1.35rem;
      }

      .brand-kicker {
        letter-spacing: 0.16em;
      }

      .tabs {
        min-height: 42px;
      }

      .tab {
        min-width: 0;
        font-size: 0.8rem;
      }

      .topbar-actions {
        width: auto;
        justify-content: flex-end;
      }

      .top-status {
        max-width: none;
      }

      .week-toolbar-actions {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto;
        gap: 8px;
        margin-left: 0;
        justify-content: stretch;
      }

      .week-toolbar-actions h2 {
        grid-column: 1 / -1;
      }

      .week-toolbar-actions select {
        width: 100%;
      }

      .today-toolbar-actions input[type="date"] {
        min-width: 0;
      }

      .calendar-grid {
        gap: 10px;
        padding: 10px;
      }

      .month-card {
        padding: 10px;
      }

      .month-name-row {
        margin-bottom: 10px;
      }

      .month-weekdays,
      .month-week-row,
      .month-rows {
        gap: 4px;
      }

      .blank-day,
      .calendar-day {
        min-height: 52px;
        border-radius: 7px;
      }

      .calendar-day {
        justify-items: center;
        gap: 0;
        padding: 7px 4px;
        text-align: center;
      }

      .calendar-day::before {
        width: 3px;
      }

      .day-number {
        font-size: 0.78rem;
      }

      .day-mini,
      .day-tags,
      .day-kpi {
        display: none;
      }
    }

    /* Event-focused workspace */
    :root {
      --bg: #f4f1e9;
      --panel: rgba(255, 253, 247, 0.94);
      --panel-strong: #fffdf8;
      --line: #ddd8cc;
      --line-strong: #a7b89a;
      --text: #20231d;
      --muted: #6f7068;
      --accent: #496d45;
      --accent-soft: #edf1e8;
      --gold: #9b742e;
      --warning: #a24b35;
      --blue: #5d7890;
      --shadow: 0 18px 44px rgba(64, 58, 46, 0.11);
      --shadow-soft: 0 10px 24px rgba(64, 58, 46, 0.07);
      --ink: #171a15;
      --paper: #fffdf8;
      --paper-soft: #f7f3eb;
      --rule: rgba(32, 35, 29, 0.12);
      --rule-strong: rgba(32, 35, 29, 0.2);
    }

    body {
      color: var(--text);
      background:
        radial-gradient(circle at 9% 0%, rgba(88, 112, 80, 0.08), transparent 26%),
        linear-gradient(90deg, rgba(32, 35, 29, 0.025) 1px, transparent 1px),
        linear-gradient(180deg, rgba(32, 35, 29, 0.022) 1px, transparent 1px),
        #f4f1e9;
      background-size: auto, 34px 34px, 34px 34px, auto;
    }

    .page {
      width: min(1560px, calc(100vw - 26px));
      padding: 14px 0 32px;
    }

    .topbar {
      grid-template-columns: minmax(260px, 0.9fr) minmax(360px, 1.2fr) minmax(180px, 0.65fr);
      min-height: 78px;
      margin-bottom: 16px;
      padding: 12px 16px 12px 18px;
      border: 1px solid rgba(32, 35, 29, 0.08);
      border-radius: 0;
      background: rgba(255, 253, 248, 0.9);
      box-shadow: 0 8px 24px rgba(64, 58, 46, 0.06);
    }

    .brand {
      gap: 5px;
    }

    .brand-mark {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.82rem;
      font-style: italic;
      font-weight: 500;
      letter-spacing: -0.055em;
      line-height: 0.95;
      transform: none;
    }

    .brand-kicker {
      color: #7b3b2d;
      font-size: 0.57rem;
      letter-spacing: 0.22em;
    }

    .tabs {
      justify-content: center;
      gap: 22px;
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 0;
    }

    .tab {
      min-width: 0;
      padding: 10px 0 12px;
      color: #73736b;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.64rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }

    .tab.active {
      color: var(--ink);
    }

    .tab.active::after {
      left: 0;
      right: 0;
      bottom: 5px;
      height: 1px;
      border-radius: 0;
      background: var(--ink);
    }

    .topbar-actions {
      gap: 8px;
    }

    .topbar-actions button,
    .icon-button {
      width: 36px;
      min-width: 36px;
      height: 36px;
      min-height: 36px;
      border-color: rgba(32, 35, 29, 0.11);
      border-radius: 999px;
      background: transparent;
      box-shadow: none;
    }

    .topbar-actions button:hover,
    .icon-button:hover {
      border-color: rgba(32, 35, 29, 0.24);
      background: #f8f5ee;
      transform: none;
    }

    .workspace {
      grid-template-columns: minmax(275px, 318px) minmax(0, 1fr);
      gap: 16px;
    }

    .context-stack {
      top: 92px;
      gap: 16px;
      max-height: calc(100vh - 108px);
    }

    .center-stage {
      gap: 16px;
    }

    .panel {
      border: 1px solid rgba(32, 35, 29, 0.09);
      border-radius: 0;
      background: rgba(255, 253, 248, 0.95);
      box-shadow: var(--shadow-soft);
    }

    .coach-rail {
      position: relative;
      padding: 19px 18px 18px 20px;
      overflow: hidden;
    }

    .coach-rail::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 3px;
      background: linear-gradient(180deg, #496d45, #9b742e 58%, #a24b35);
    }

    .coach-rail-head {
      align-items: flex-start;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--rule);
    }

    .coach-rail h2,
    .month-rail h2 {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 2.08rem;
      font-weight: 500;
      letter-spacing: -0.07em;
      line-height: 0.98;
    }

    .eyebrow,
    .section-title,
    .rail-note-label span,
    .toolbar h2,
    .stat-label,
    .progress-label {
      color: #7c7a70;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.56rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }

    .eyebrow {
      color: var(--warning);
      margin-bottom: 8px;
    }

    .day-nav {
      gap: 4px;
    }

    .day-nav .icon-button {
      width: 28px;
      min-width: 28px;
      height: 28px;
      border: 0;
      background: transparent;
    }

    .coach-rail-content {
      gap: 16px;
      padding-top: 14px;
    }

    .rail-section {
      gap: 11px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--rule);
    }

    .session-card,
    .actual-card {
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }

    .session-card {
      grid-template-columns: 24px minmax(0, 1fr) auto;
      gap: 10px;
      padding: 0;
    }

    .session-icon {
      width: 24px;
      height: 24px;
      color: var(--accent);
      background: #edf1e8;
      font-size: 0.62rem;
    }

    .session-card strong,
    .actual-card strong {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.24rem;
      font-weight: 500;
      letter-spacing: -0.04em;
      line-height: 1.08;
    }

    .session-card p,
    .actual-card p,
    .sidebar-copy {
      color: #68685f;
      font-size: 0.74rem;
      line-height: 1.45;
    }

    .actual-card {
      gap: 8px;
      padding: 0 0 0 12px;
      border-left: 2px solid #a24b35;
    }

    .rail-mini-grid,
    .recovery-list {
      gap: 6px;
    }

    .rail-mini-grid div,
    .recovery-list span {
      border: 1px solid rgba(32, 35, 29, 0.08);
      border-radius: 0;
      background: #faf7f0;
      padding: 9px 10px;
    }

    .rail-mini-grid strong,
    .recovery-list strong {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.28rem;
      font-weight: 500;
      letter-spacing: -0.05em;
    }

    .rail-mini-grid span,
    .recovery-list span {
      color: #7c7a70;
      font-size: 0.48rem;
      letter-spacing: 0.12em;
    }

    textarea {
      border: 1px solid rgba(32, 35, 29, 0.11);
      border-radius: 0;
      background: #fbf8f1;
      color: var(--ink);
      font-size: 0.78rem;
    }

    .toolbar {
      min-height: 58px;
      padding: 15px 18px 12px;
      border-bottom: 1px solid var(--rule);
      background: transparent;
    }

    .toolbar h2 {
      color: #5f6158;
      font-size: 0.54rem;
      letter-spacing: 0.19em;
    }

    .week-toolbar-actions {
      display: grid;
      grid-template-columns: auto auto minmax(320px, 1fr) auto;
      align-items: center;
      gap: 9px;
      width: 100%;
    }

    .week-toolbar-actions select,
    .toolbar select,
    .toolbar input[type="date"] {
      border: 0;
      border-bottom: 1px solid rgba(32, 35, 29, 0.18);
      border-radius: 0;
      background: transparent;
      color: var(--ink);
      padding: 7px 26px 7px 0;
      font-size: 0.78rem;
    }

    .week-arrow {
      border: 0;
      background: transparent;
    }

    #weeks-view {
      background: rgba(255, 253, 248, 0.95);
    }

    .week-card {
      padding: 22px 22px 20px;
    }

    .week-head {
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      margin-bottom: 18px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--rule);
    }

    .week-title {
      margin-bottom: 7px;
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: clamp(2.1rem, 3vw, 3.2rem);
      font-weight: 500;
      letter-spacing: -0.085em;
      line-height: 0.95;
    }

    .week-focus {
      max-width: 820px;
      color: #5e6057;
      font-size: 0.87rem;
      line-height: 1.55;
    }

    .week-focus strong {
      color: var(--ink);
      font-weight: 600;
    }

    .pill-row {
      align-items: flex-start;
      gap: 6px;
      padding-top: 3px;
    }

    .pill,
    .phase-chip,
    .complete-dot,
    .quiet-dot {
      border: 1px solid rgba(73, 109, 69, 0.18);
      border-radius: 999px;
      color: var(--accent);
      background: #eef2ea;
      font-size: 0.48rem;
      letter-spacing: 0.14em;
      padding: 6px 8px;
    }

    .pill.warn,
    .quiet-dot {
      border-color: rgba(155, 116, 46, 0.18);
      color: #8b6827;
      background: #f6efe1;
    }

    .week-overview {
      gap: 16px;
      margin-bottom: 16px;
    }

    .summary-grid {
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 0;
      border-top: 1px solid var(--rule);
      border-bottom: 1px solid var(--rule);
    }

    .summary-card {
      min-height: 86px;
      border: 0;
      border-right: 1px solid var(--rule);
      border-radius: 0;
      background: transparent;
      padding: 15px 15px 13px 0;
      margin-right: 15px;
    }

    .summary-card:last-child {
      border-right: 0;
      margin-right: 0;
    }

    .summary-value {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.95rem;
      font-weight: 500;
      letter-spacing: -0.075em;
      line-height: 0.95;
      margin-bottom: 9px;
    }

    .stat-label {
      color: #77786e;
      font-size: 0.47rem;
      letter-spacing: 0.14em;
    }

    .progress-row {
      gap: 8px;
      padding-top: 1px;
    }

    .progress-label {
      color: #74766d;
      font-size: 0.49rem;
    }

    .progress-track {
      height: 7px;
      border: 0;
      border-radius: 0;
      background: #e7e1d4;
    }

    .progress-fill {
      border-radius: 0;
      background: linear-gradient(90deg, #476d44, #7d9b70);
    }

    .progress-min {
      width: 1px;
      background: #9b742e;
    }

    .week-detail {
      color: #68695f;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.02rem;
      line-height: 1.35;
    }

    .week-days {
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 0;
      border-top: 1px solid var(--rule-strong);
      border-bottom: 1px solid var(--rule-strong);
    }

    .week-day {
      position: relative;
      min-height: 332px;
      height: 332px;
      border: 0;
      border-right: 1px solid var(--rule);
      border-radius: 0;
      background: transparent;
      padding: 14px 12px 12px;
      gap: 9px;
    }

    .week-day:last-child {
      border-right: 0;
    }

    .week-day::before {
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 3px;
      background: transparent;
    }

    .week-day.selected {
      border-color: transparent;
      background: #f5f4ed;
      box-shadow: none;
    }

    .week-day.selected::before {
      background: var(--accent);
    }

    .week-day.has-ride {
      box-shadow: none;
    }

    .week-day.has-ride::after {
      content: "";
      position: absolute;
      left: 12px;
      right: 12px;
      bottom: 0;
      height: 2px;
      background: rgba(73, 109, 69, 0.48);
    }

    .week-day.hard-day {
      background: linear-gradient(180deg, rgba(155, 116, 46, 0.045), transparent 62%);
    }

    .date-label {
      color: #6f7168;
      font-size: 0.51rem;
      letter-spacing: 0.14em;
    }

    .week-day .actual {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.1rem;
      font-weight: 500;
      letter-spacing: -0.035em;
      line-height: 1.12;
      -webkit-line-clamp: 3;
    }

    .week-day .planned,
    .planned,
    .actual,
    .metric-line,
    .empty-ride-stats {
      color: #66675f;
      font-size: 0.68rem;
      line-height: 1.4;
    }

    .week-day-title-stack .planned {
      color: #6f7068;
      font-size: 0.64rem;
      line-height: 1.35;
    }

    .week-day-strava {
      margin-top: -2px;
      font-size: 0.58rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .actual-link,
    .text-link {
      color: var(--accent);
      text-decoration-color: rgba(73, 109, 69, 0.35);
    }

    .week-stat-chip-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 4px;
    }

    .week-stat-chip {
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 5px 0;
    }

    .week-stat-chip strong {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1rem;
      font-weight: 500;
      letter-spacing: -0.05em;
    }

    .week-stat-chip span {
      display: block;
      color: #88897e;
      font-size: 0.42rem;
      letter-spacing: 0.11em;
    }

    .load-qualifier {
      display: block;
      margin-top: 3px;
      color: #925235;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: .52rem;
      font-weight: 500;
      letter-spacing: 0;
      line-height: 1.3;
    }

    .day-spark {
      height: 46px;
      margin: 0;
    }

    .day-spark .spark-segment {
      stroke-width: 2.2;
    }

    .day-spark .spark-fill {
      fill: rgba(73, 109, 69, 0.055);
    }

    .day-spark .spark-elevation-fill {
      fill: rgba(32, 35, 29, 0.08);
    }

    .day-spark .spark-elevation-line {
      stroke: rgba(32, 35, 29, 0.16);
      stroke-width: 1;
    }

    .day-spark .spark-tag {
      color: #8d8c80;
      font-size: 0.42rem;
    }

    .day-spark.bars {
      height: 44px;
      padding-top: 10px;
    }

    .day-spark .spark-bars {
      gap: 2px;
    }

    .day-spark.bars i {
      border-radius: 0;
      background: #b99348;
    }

    .day-spark.empty {
      height: 44px;
      border-bottom: 1px solid rgba(32, 35, 29, 0.12);
    }

    .week-day-signal {
      min-width: 18px;
      min-height: 18px;
      padding: 2px 5px;
      border-radius: 999px;
      background: #d9d6cc;
      color: #fff;
      font-size: 0.47rem;
      letter-spacing: 0.04em;
    }

    .interval-chip,
    .event-chip {
      border-radius: 999px;
      font-size: 0.45rem;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }

    .interval-chip {
      color: #815f22;
      border-color: rgba(155, 116, 46, 0.2);
      background: #f5efe2;
    }

    .event-chip {
      color: #8a3e2d;
      border-color: rgba(162, 75, 53, 0.2);
      background: #f7ede8;
    }

    .week-load-chart,
    .today-load-card {
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 18px 0 0;
    }

    .week-load-chart-head {
      padding-bottom: 6px;
      border-bottom: 1px solid var(--rule);
    }

    .week-load-chart h4,
    .today-card h4 {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.1rem;
      font-weight: 500;
      letter-spacing: -0.04em;
    }

    .chart-legend {
      color: #77786f;
      font-size: 0.48rem;
      letter-spacing: 0.13em;
    }

    .chart-legend i {
      height: 1px;
      background: var(--accent);
    }

    .chart-legend .planned i {
      background: #a8aaa0;
    }

    .week-load-chart .grid-line,
    .today-load-card .grid-line {
      stroke: #e8e3d8;
    }

    .week-load-chart .planned-line,
    .today-load-card .planned-line {
      stroke: #aaa99e;
      stroke-width: 1.7;
      stroke-dasharray: 4 5;
    }

    .week-load-chart .actual-line,
    .today-load-card .actual-line {
      stroke: var(--accent);
      stroke-width: 2.4;
    }

    .week-load-chart .actual-area,
    .today-load-card .actual-area {
      fill: rgba(73, 109, 69, 0.06);
    }

    .week-load-chart .chart-dot,
    .today-load-card .chart-dot {
      fill: var(--accent);
      stroke: var(--paper);
    }

    .month-rail {
      position: relative;
      gap: 18px;
      padding: 18px 20px 16px;
      overflow: hidden;
    }

    .month-rail::before {
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 3px;
      background: var(--warning);
    }

    .month-rail-head {
      align-items: flex-end;
      padding-top: 2px;
    }

    .month-rail h2 {
      font-size: 2rem;
    }

    .race-marker-list {
      gap: 10px;
    }

    .race-marker {
      min-width: 148px;
      padding: 0 0 0 10px;
      border: 0;
      border-left: 2px solid var(--warning);
      background: transparent;
      border-radius: 0;
      box-shadow: none;
    }

    .race-marker:hover {
      background: transparent;
      transform: none;
    }

    .race-marker span,
    .race-marker small,
    .quiet-context {
      color: #7a7b71;
      font-size: 0.46rem;
      letter-spacing: 0.12em;
    }

    .race-marker strong {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1rem;
      font-weight: 500;
      letter-spacing: -0.035em;
    }

    .month-strip-days {
      position: relative;
      gap: 4px;
      padding-top: 8px;
    }

    .month-strip-days::before {
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      top: 29px;
      height: 1px;
      background: #d9d4c7;
    }

    .month-strip-day {
      position: relative;
      z-index: 1;
      height: 34px;
      gap: 3px;
      padding: 0;
      border-radius: 0;
    }

    .month-strip-day span {
      color: #77786f;
      font-size: 0.48rem;
    }

    .month-strip-day i {
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: #c9c6bb;
    }

    .month-strip-day.has-ride i {
      background: var(--accent);
    }

    .month-strip-day.interval i {
      background: var(--gold);
    }

    .month-strip-day.race i {
      width: 9px;
      height: 9px;
      background: var(--warning);
    }

    .month-strip-day.selected {
      background: transparent;
    }

    .month-strip-day.selected::after {
      content: "";
      position: absolute;
      left: 50%;
      bottom: -3px;
      width: 16px;
      height: 2px;
      background: var(--accent);
      transform: translateX(-50%);
    }

    .month-strip-day.selected span {
      color: var(--ink);
    }

    .today-dashboard {
      grid-template-columns: minmax(0, 1.08fr) minmax(280px, 0.92fr);
      gap: 0;
      padding: 0;
    }

    .today-card {
      border: 0;
      border-right: 1px solid var(--rule);
      border-radius: 0;
      background: transparent;
      padding: 22px;
    }

    .today-card:last-child {
      border-right: 0;
    }

    .today-card h3 {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: clamp(2.4rem, 4vw, 4.2rem);
      font-weight: 500;
      letter-spacing: -0.09em;
      line-height: 0.95;
    }

    .today-plan-copy {
      color: #5f6058;
      font-size: 0.93rem;
      line-height: 1.55;
    }

    .today-grid {
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0;
      border-top: 1px solid var(--rule);
      border-bottom: 1px solid var(--rule);
    }

    .today-grid .summary-card {
      min-height: 82px;
    }

    .ride-sidebar {
      border-radius: 0;
      background: rgba(255, 253, 248, 0.96);
      box-shadow: var(--shadow-soft);
    }

    .sidebar-section {
      border-color: var(--rule);
      border-radius: 0;
      background: transparent;
      padding: 12px 0;
    }

    .activity-card,
    .coach-note-card {
      border-radius: 0;
      background: #fbf8f1;
    }

    .activity-card {
      border-color: var(--rule);
    }

    .coach-note-card {
      border-color: rgba(155, 116, 46, 0.18);
      background: #f8f0df;
    }

    .calendar-grid {
      gap: 18px;
      padding: 18px;
    }

    .month-card {
      border-color: var(--rule);
      border-radius: 0;
      background: transparent;
      padding: 16px 0 0;
    }

    .month-name-row {
      padding: 0 16px 13px;
      border-bottom: 1px solid var(--rule);
    }

    .month-name {
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.55rem;
      font-weight: 500;
      letter-spacing: -0.06em;
      text-transform: none;
    }

    .month-weekdays,
    .month-week-row {
      gap: 0;
    }

    .weekday,
    .week-total-heading {
      color: #77786e;
      font-size: 0.47rem;
      letter-spacing: 0.14em;
    }

    .calendar-day,
    .blank-day,
    .calendar-week-stat {
      border-radius: 0;
    }

    .calendar-day {
      min-height: 118px;
      border-color: var(--rule);
      border-width: 0 1px 1px 0;
      background: rgba(255, 253, 248, 0.82);
    }

    .calendar-day.today,
    .calendar-day.selected {
      background: #f5f4ed;
    }

    .calendar-day.selected {
      outline: 0;
      box-shadow: inset 0 0 0 1px var(--accent);
    }

    .calendar-day.interval-day {
      background: #fbf5e9;
    }

    .calendar-day.race-day,
    .calendar-day.race-road-day,
    .calendar-day.race-dirt-day {
      background: #fbefea;
    }

    .calendar-day.big-day:not(.race-day) {
      background: #fbf5e9;
    }

    .day-mini {
      color: #5f6158;
      font-size: 0.78rem;
    }

    .day-kpi {
      color: #83837a;
      font-size: 0.48rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }

    @media (max-width: 1320px) {
      .workspace {
        grid-template-columns: minmax(250px, 300px) minmax(0, 1fr);
      }

      .summary-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .summary-card:nth-child(3) {
        border-right: 0;
        margin-right: 0;
      }

      .summary-card:nth-child(n+4) {
        border-top: 1px solid var(--rule);
      }

      .week-days {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }

      .week-day:nth-child(4n) {
        border-right: 0;
      }

      .week-day:nth-child(n+5) {
        border-top: 1px solid var(--rule);
      }
    }

    @media (max-width: 960px) {
      .topbar {
        grid-template-columns: minmax(0, 1fr) auto;
        grid-template-areas:
          "brand actions"
          "tabs tabs";
        gap: 10px;
      }

      .tabs {
        justify-content: space-between;
        gap: 12px;
      }

      .workspace,
      .week-head,
      .recovery-card {
        grid-template-columns: 1fr;
      }

      .week-days {
        grid-template-columns: 1fr;
        border-top: 1px solid var(--rule);
      }

      .week-day,
      .week-day:nth-child(4n) {
        min-height: 250px;
        height: 250px;
        border-right: 0;
        border-top: 1px solid var(--rule);
      }

      .week-day:first-child {
        border-top: 0;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .summary-card,
      .summary-card:nth-child(3) {
        border-top: 0;
        border-right: 1px solid var(--rule);
        margin-right: 12px;
      }

      .summary-card:nth-child(2n) {
        border-right: 0;
        margin-right: 0;
      }

      .summary-card:nth-child(n+3) {
        border-top: 1px solid var(--rule);
      }

      .today-dashboard {
        grid-template-columns: 1fr;
      }

      .today-card {
        border-right: 0;
        border-bottom: 1px solid var(--rule);
      }

      .today-card:last-child {
        border-bottom: 0;
      }

      .month-rail-head {
        align-items: flex-start;
      }
    }

    @media (max-width: 640px) {
      .page {
        width: calc(100vw - 16px);
        padding-top: 8px;
      }

      .topbar {
        min-height: 0;
        padding: 12px;
      }

      .brand-mark {
        font-size: 1.52rem;
      }

      .tabs {
        overflow-x: auto;
      }

      .tab {
        flex: 0 0 auto;
        font-size: 0.56rem;
      }

      .week-card {
        padding: 16px;
      }

      .week-title {
        font-size: 2.2rem;
      }

      .week-head {
        gap: 12px;
      }

      .week-toolbar-actions {
        grid-template-columns: auto minmax(0, 1fr) auto;
      }

      .week-toolbar-actions h2 {
        grid-column: 1 / -1;
      }

      .coach-rail {
        padding: 17px 16px 16px 18px;
      }

      .coach-rail h2,
      .month-rail h2 {
        font-size: 1.75rem;
      }

      .summary-value {
        font-size: 1.6rem;
      }

      .month-rail {
        padding: 16px;
      }
    }

    /* Coach context panel */
    .page {
      width: min(1600px, calc(100vw - 20px));
      padding-top: 10px;
    }

    .topbar {
      grid-template-columns: minmax(210px, 0.75fr) minmax(300px, 1fr) minmax(140px, 0.5fr);
      min-height: 66px;
      margin-bottom: 10px;
      padding: 10px 14px 10px 16px;
      box-shadow: none;
    }

    .brand-mark {
      font-size: 1.58rem;
    }

    .brand-kicker {
      font-size: 0.5rem;
      letter-spacing: 0.18em;
    }

    .tabs {
      gap: 18px;
    }

    .tab {
      font-size: 0.54rem;
      letter-spacing: 0.15em;
    }

    .workspace {
      grid-template-columns: minmax(242px, 274px) minmax(0, 1fr);
      gap: 10px;
    }

    .context-stack {
      top: 82px;
      gap: 10px;
      max-height: calc(100vh - 92px);
    }

    .center-stage {
      gap: 10px;
    }

    .coach-rail {
      padding: 16px 14px 15px 17px;
    }

    .coach-rail::before {
      width: 2px;
    }

    .coach-rail h2 {
      font-size: 1.62rem;
    }

    .coach-rail-head {
      padding-bottom: 12px;
    }

    .coach-rail-content {
      gap: 12px;
      padding-top: 12px;
    }

    .rail-section {
      gap: 8px;
      padding-bottom: 12px;
    }

    .session-card {
      grid-template-columns: 20px minmax(0, 1fr) auto;
      gap: 8px;
    }

    .session-icon {
      width: 20px;
      height: 20px;
      font-size: 0.52rem;
    }

    .session-card strong,
    .actual-card strong {
      font-size: 1rem;
    }

    .session-card p,
    .actual-card p,
    .sidebar-copy {
      font-size: 0.66rem;
      line-height: 1.4;
    }

    .rail-mini-grid div,
    .recovery-list span {
      padding: 7px 8px;
    }

    .rail-mini-grid strong,
    .recovery-list strong {
      font-size: 1rem;
    }

    .coach-presence {
      display: grid;
      gap: 11px;
      position: relative;
      padding: 14px 14px 15px 16px;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(73, 109, 69, 0.035), transparent),
        rgba(255, 253, 248, 0.95);
    }

    .coach-presence::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 2px;
      background: #496d45;
    }

    .coach-presence-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }

    .coach-presence h3 {
      margin: 0;
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.34rem;
      font-weight: 500;
      letter-spacing: -0.05em;
      line-height: 1.05;
    }

    .coach-presence-dot {
      width: 7px;
      height: 7px;
      margin-top: 3px;
      border-radius: 999px;
      background: #496d45;
      box-shadow: 0 0 0 4px rgba(73, 109, 69, 0.11);
    }

    .coach-presence-copy p {
      margin: 0;
      color: #5f6158;
      font-size: 0.69rem;
      line-height: 1.42;
    }

    .coach-prompt-list {
      display: grid;
      gap: 4px;
    }

    .coach-prompt {
      width: 100%;
      border: 0;
      border-top: 1px solid var(--rule);
      border-radius: 0;
      background: transparent;
      color: #59605a;
      padding: 7px 0 0;
      box-shadow: none;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.47rem;
      letter-spacing: 0.09em;
      text-align: left;
      text-transform: uppercase;
    }

    .coach-prompt:hover {
      color: var(--accent);
      background: transparent;
      transform: none;
    }

    .toolbar {
      min-height: 46px;
      padding: 11px 15px 9px;
    }

    .week-card {
      padding: 18px 18px 16px;
    }

    .week-head {
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      margin-bottom: 14px;
      padding-bottom: 14px;
    }

    .week-title {
      font-size: clamp(2.2rem, 4vw, 4.2rem);
    }

    .week-focus {
      max-width: 900px;
      font-size: 0.82rem;
      line-height: 1.48;
    }

    .week-overview {
      margin-bottom: 12px;
    }

    .summary-grid {
      border-top: 0;
      border-bottom: 1px solid var(--rule);
    }

    .summary-card {
      min-height: 64px;
      padding: 9px 10px 9px 0;
      margin-right: 10px;
    }

    .summary-value {
      font-size: 1.5rem;
      margin-bottom: 6px;
    }

    .progress-row {
      gap: 6px;
    }

    .week-detail {
      font-size: 0.9rem;
    }

    .week-days {
      border-top: 1px solid var(--rule-strong);
    }

    .week-day {
      min-height: 356px;
      height: 356px;
      padding: 12px 10px 10px;
      gap: 8px;
    }

    .week-day .actual {
      font-size: 1.02rem;
      line-height: 1.08;
    }

    .week-day .planned,
    .planned,
    .actual,
    .metric-line,
    .empty-ride-stats {
      font-size: 0.62rem;
    }

    .day-spark {
      height: 42px;
    }

    .week-load-chart {
      padding-top: 14px;
    }

    .week-load-chart h4,
    .today-card h4 {
      font-size: 0.98rem;
    }

    .month-rail {
      gap: 14px;
      padding: 16px 18px 14px;
    }

    .month-rail h2 {
      font-size: 1.7rem;
    }

    .race-marker {
      min-width: 130px;
    }

    .race-marker strong {
      font-size: 0.86rem;
    }

    .month-strip-day {
      height: 30px;
    }

    .month-strip-day span {
      font-size: 0.43rem;
    }

    body[data-view="today"] .coach-presence,
    body[data-view="connections"] .coach-presence,
    body[data-view="settings"] .coach-presence {
      display: none;
    }

    @media (min-width: 1321px) {
      body[data-view="weeks"] .workspace {
        grid-template-columns: minmax(236px, 252px) minmax(0, 1fr);
      }

      body[data-view="weeks"] .context-stack {
        max-height: none;
      }
    }

    @media (max-width: 960px) {
      .coach-presence {
        display: none;
      }
    }

    /* Weekly plan overview */
    .week-card {
      padding: 0;
    }

    .week-desk {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(225px, 0.26fr);
      gap: 0;
      border-bottom: 1px solid var(--rule);
      background:
        linear-gradient(90deg, rgba(250, 248, 242, 0.98), rgba(255, 253, 248, 0.94)),
        var(--paper);
    }

    .week-desk-main {
      min-width: 0;
      padding: 22px 24px 19px 22px;
    }

    .week-desk-kicker,
    .week-stance-label {
      color: #8a4c35;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.49rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      line-height: 1;
      text-transform: uppercase;
    }

    .week-desk-kicker {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
      color: #7a766c;
    }

    .week-desk-kicker::before {
      content: "";
      display: block;
      width: 20px;
      height: 1px;
      background: #8a4c35;
    }

    .week-title {
      max-width: 920px;
      margin: 0;
      color: #141815;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: clamp(2.65rem, 4.1vw, 4.72rem);
      font-weight: 500;
      letter-spacing: -0.075em;
      line-height: 0.93;
    }

    .week-focus {
      max-width: 780px;
      margin: 13px 0 0;
      color: #5c5a52;
      font-size: 0.78rem;
      line-height: 1.48;
    }

    .week-focus strong {
      color: #23251f;
      font-weight: 650;
    }

    .week-stance {
      display: grid;
      align-content: space-between;
      min-width: 0;
      padding: 22px 18px 18px;
      border-left: 1px solid var(--rule);
      background:
        linear-gradient(180deg, rgba(73, 109, 69, 0.055), rgba(73, 109, 69, 0.014)),
        rgba(250, 249, 245, 0.72);
    }

    .week-stance-copy {
      display: grid;
      gap: 10px;
    }

    .week-stance strong {
      display: block;
      color: #263626;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.34rem;
      font-weight: 500;
      letter-spacing: -0.065em;
      line-height: 1.02;
    }

    .week-stance p {
      max-width: 24ch;
      margin: 0;
      color: #69675f;
      font-size: 0.64rem;
      line-height: 1.45;
    }

    .week-stance-foot {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 20px;
      padding-top: 10px;
      border-top: 1px solid rgba(73, 109, 69, 0.16);
      color: #62735e;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.44rem;
      letter-spacing: 0.11em;
      text-transform: uppercase;
    }

    .week-overview {
      display: grid;
      gap: 0;
      margin: 0;
      padding: 0 16px;
      border-bottom: 1px solid var(--rule);
      background: rgba(255, 253, 248, 0.74);
    }

    .summary-grid {
      display: grid;
      grid-template-columns: 1.05fr 0.9fr 0.95fr 0.9fr 0.95fr 1.05fr;
      gap: 0;
      border: 0;
    }

    .summary-card {
      min-height: 72px;
      margin: 0;
      padding: 13px 12px 11px;
      border: 0;
      border-right: 1px solid var(--rule);
      background: transparent;
    }

    .summary-card:first-child {
      padding-left: 0;
    }

    .summary-card:last-child {
      border-right: 0;
    }

    .summary-value {
      margin-bottom: 7px;
      color: #20251f;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.54rem;
      font-weight: 500;
      letter-spacing: -0.08em;
      line-height: 1;
    }

    .week-progress-band {
      display: grid;
      grid-template-columns: auto minmax(120px, 1fr) auto;
      gap: 9px;
      align-items: center;
      padding: 10px 0 11px;
      border-top: 1px solid var(--rule);
    }

    .week-progress-band .progress-label {
      white-space: nowrap;
    }

    .week-progress-note {
      color: #7b766c;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.46rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .week-detail {
      margin: 0;
      padding: 10px 0 11px;
      border-top: 1px solid var(--rule);
      color: #55584f;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 0.96rem;
      line-height: 1.2;
    }

    .event-row {
      margin: 0;
      padding: 9px 16px 0;
      background: rgba(255, 253, 248, 0.74);
    }

    .week-days {
      padding: 9px 16px 0;
      border-top: 0;
      background: rgba(255, 253, 248, 0.74);
    }

    .week-day {
      min-height: 326px;
      height: 326px;
      border-top: 2px solid transparent;
      border-right: 1px solid var(--rule);
      border-bottom: 0;
      border-left: 0;
      border-radius: 0;
      background: rgba(255, 253, 248, 0.72);
    }

    .week-day:first-child {
      border-left: 1px solid var(--rule);
    }

    .week-day.selected {
      border-top-color: #496d45;
      background: rgba(242, 243, 235, 0.98);
      box-shadow: none;
    }

    .week-day.has-ride,
    .week-day.selected.has-ride {
      box-shadow: inset 0 -3px 0 rgba(73, 109, 69, 0.44);
    }

    .week-load-chart {
      margin: 0 16px 16px;
      border-right: 0;
      border-bottom: 0;
      border-left: 0;
      border-radius: 0;
      background: transparent;
      padding: 15px 0 0;
    }

    .coach-presence {
      border-color: rgba(73, 109, 69, 0.22);
      background:
        linear-gradient(180deg, rgba(73, 109, 69, 0.055), rgba(73, 109, 69, 0.01)),
        rgba(250, 249, 244, 0.97);
    }

    .coach-presence h3 {
      max-width: 11ch;
      font-size: 1.55rem;
      line-height: 0.95;
    }

    .coach-presence-copy p {
      max-width: 24ch;
      color: #65695e;
      font-size: 0.67rem;
    }

    /* Primary desk: dark coach spine + calm editorial work surface. */
    body.primary-shell {
      --coach-spine: #213a35;
      --coach-spine-deep: #1a302c;
      --coach-canvas: #f5f5f1;
      --coach-paper: #faf9f4;
      --coach-paper-soft: #f6f6f1;
    }

    body.primary-shell .page {
      width: 100%;
      max-width: none;
      padding: 0;
      background: var(--coach-canvas);
    }

    body.primary-shell .topbar {
      border-bottom: 1px solid transparent;
      background: transparent;
      width: calc(100% - 262px);
      margin-left: 262px;
      box-shadow: none;
      backdrop-filter: none;
      transition: background-color 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
    }

    body.primary-shell.has-scrolled .topbar {
      border-bottom-color: transparent;
      background: transparent;
      box-shadow: none;
      backdrop-filter: none;
    }

    .sr-status {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      white-space: nowrap;
      border: 0;
    }

    body.primary-shell .topbar .brand {
      visibility: hidden;
    }

    body.primary-shell .workspace {
      grid-template-columns: minmax(244px, 262px) minmax(0, 1fr);
      gap: 0;
      padding-top: 0;
      margin-top: -92px;
    }

    body.primary-shell .context-stack {
      display: grid;
      align-content: start;
      top: 0;
      height: 100vh;
      gap: 0;
      max-height: 100vh;
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-color: rgba(229, 235, 219, 0.24) transparent;
      scrollbar-width: thin;
      background: var(--coach-spine);
    }

    body.primary-shell .context-stack > * {
      border: 0;
      border-radius: 0;
      box-shadow: none;
    }

    .sidebar-brand {
      display: none;
    }

    body.primary-shell .sidebar-brand {
      display: grid;
      align-content: start;
      gap: 4px;
      padding: 22px 20px 18px;
      border-bottom: 1px solid rgba(255,255,255,.16);
      background: var(--coach-spine);
    }

    .sidebar-brand strong {
      color: #f7f4e8;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.22rem;
      font-weight: 500;
      letter-spacing: -.04em;
      text-transform: uppercase;
    }

    .sidebar-brand span {
      color: #c7d3b6;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: .46rem;
      letter-spacing: .15em;
      text-transform: uppercase;
    }

    body.primary-shell .coach-rail {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      align-content: start;
      min-width: 0;
      overflow: visible;
      color: rgba(255, 255, 255, 0.9);
      background: var(--coach-spine);
      padding: 21px 20px 17px;
    }

    body.primary-shell .coach-rail::before {
      display: none;
    }

    body.primary-shell .coach-rail-content {
      align-content: start;
      min-width: 0;
      max-width: 100%;
    }

    body.primary-shell .coach-rail .rail-section,
    body.primary-shell .coach-rail .section-title-row {
      min-width: 0;
      max-width: 100%;
    }

    body.primary-shell .coach-rail .section-title-row {
      flex-wrap: wrap;
      gap: 6px 10px;
    }

    body.primary-shell .coach-rail .session-card {
      grid-template-columns: 20px minmax(0, 1fr);
      grid-template-areas: "icon copy" ". duration";
      gap: 6px 8px;
      min-width: 0;
      max-width: 100%;
    }

    body.primary-shell .coach-rail .session-icon {
      grid-area: icon;
    }

    body.primary-shell .coach-rail .session-copy {
      grid-area: copy;
      min-width: 0;
    }

    body.primary-shell .coach-rail .session-duration {
      grid-area: duration;
      justify-self: start;
      max-width: 100%;
      white-space: normal;
      overflow-wrap: anywhere;
    }

    body.primary-shell .coach-rail .eyebrow,
    body.primary-shell .coach-rail .section-title,
    body.primary-shell .coach-rail .phase-chip,
    body.primary-shell .coach-rail .session-card span,
    body.primary-shell .coach-rail .rail-mini-grid span {
      color: rgba(218, 227, 211, 0.68);
    }

    body.primary-shell .coach-rail h2,
    body.primary-shell .coach-rail .session-card strong,
    body.primary-shell .coach-rail .rail-mini-grid strong {
      color: #f7f4e8;
    }

    body.primary-shell .coach-rail h2 {
      font-size: 1.86rem;
      line-height: 0.98;
    }

    body.primary-shell .coach-rail-head,
    body.primary-shell .rail-section {
      border-color: rgba(255, 255, 255, 0.14);
    }

    body.primary-shell .coach-rail .icon-button {
      color: rgba(255, 255, 255, 0.7);
      border-color: rgba(255, 255, 255, 0.16);
      background: rgba(255, 255, 255, 0.03);
    }

    body.primary-shell .coach-rail .today-return {
      color: rgba(232, 237, 220, 0.84);
      border-color: rgba(218, 227, 211, 0.2);
      background: rgba(218, 227, 211, 0.07);
    }

    body.primary-shell .coach-rail .today-return .target-dot {
      fill: currentColor;
      stroke: none;
    }

    body.primary-shell .coach-rail .session-card,
    body.primary-shell .coach-rail .rail-mini-grid div {
      border-color: rgba(255, 255, 255, 0.1);
      background: rgba(255, 255, 255, 0.028);
    }

    body.primary-shell .coach-rail .session-card p {
      color: rgba(235, 239, 226, 0.68);
      line-height: 1.36;
    }

    body.primary-shell .coach-rail .phase-chip {
      border-color: rgba(255, 255, 255, 0.12);
      background: rgba(255, 255, 255, 0.05);
      min-width: 0;
      max-width: 100%;
      white-space: normal;
      overflow-wrap: anywhere;
      line-height: 1.35;
    }

    body.primary-shell .rail-detail {
      display: block;
      margin-top: 2px;
      padding-bottom: 0;
    }

    body.primary-shell .rail-detail-summary {
      display: grid;
      gap: 11px;
      list-style: none;
      cursor: pointer;
    }

    body.primary-shell .rail-detail-summary::-webkit-details-marker {
      display: none;
    }

    body.primary-shell .rail-detail-summary::marker {
      content: "";
    }

    body.primary-shell .rail-detail-summary:focus-visible {
      outline: 1px solid rgba(218, 227, 211, 0.42);
      outline-offset: 6px;
    }

    body.primary-shell .rail-detail-cue {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 2px;
      padding-top: 9px;
      border-top: 1px solid rgba(255, 255, 255, 0.1);
      color: rgba(218, 227, 211, 0.66);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.44rem;
      font-weight: 700;
      letter-spacing: 0.15em;
      text-transform: uppercase;
    }

    body.primary-shell .rail-detail-cue::after {
      content: "+";
      color: rgba(247, 244, 232, 0.74);
      font-size: 0.72rem;
      line-height: 1;
    }

    body.primary-shell .rail-detail[open] .rail-detail-cue::after {
      content: "-";
    }

    body.primary-shell .rail-detail-cue .expanded {
      display: none;
    }

    body.primary-shell .rail-detail[open] .rail-detail-cue .collapsed {
      display: none;
    }

    body.primary-shell .rail-detail[open] .rail-detail-cue .expanded {
      display: inline;
    }

    body.primary-shell .rail-detail-body {
      display: grid;
      gap: 10px;
      margin-top: 14px;
      padding-top: 13px;
      border-top: 1px solid rgba(255, 255, 255, 0.12);
    }

    body.primary-shell .rail-detail-body .sidebar-section,
    body.primary-shell .rail-coach-notes {
      display: grid;
      gap: 7px;
      border: 1px solid rgba(255, 255, 255, 0.09);
      border-radius: 0;
      background: rgba(255, 255, 255, 0.025);
      padding: 10px;
    }

    body.primary-shell .rail-detail-body .sidebar-section h4,
    body.primary-shell .rail-detail-body .stat-section-title,
    body.primary-shell .rail-coach-notes h4 {
      color: rgba(218, 227, 211, 0.64);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.46rem;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    body.primary-shell .rail-detail-body .sidebar-copy,
    body.primary-shell .rail-detail-body .activity-meta {
      color: rgba(235, 239, 226, 0.7);
      font-size: 0.62rem;
      line-height: 1.42;
    }

    body.primary-shell .rail-detail-body .stat-row {
      grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
      gap: 6px;
      padding: 5px 0;
      border-bottom-color: rgba(255, 255, 255, 0.08);
    }

    body.primary-shell .rail-detail-body .stat-row span {
      color: rgba(218, 227, 211, 0.55);
      font-size: 0.46rem;
    }

    body.primary-shell .rail-detail-body .stat-row strong {
      color: rgba(247, 244, 232, 0.86);
      font-size: 0.64rem;
      font-weight: 500;
    }

    body.primary-shell .rail-detail-body .activity-card,
    body.primary-shell .rail-detail-body .coach-note-card,
    body.primary-shell .rail-coach-notes .coach-note-card {
      gap: 6px;
      border-color: rgba(255, 255, 255, 0.09);
      border-radius: 0;
      background: rgba(255, 255, 255, 0.03);
      padding: 9px;
    }

    body.primary-shell .rail-detail-body .activity-card a,
    body.primary-shell .rail-detail-body .coach-note-card a,
    body.primary-shell .rail-coach-notes .coach-note-card a {
      color: #f5f3e7;
      font-size: 0.66rem;
      text-decoration-color: rgba(218, 227, 211, 0.4);
    }

    body.primary-shell .rail-coach-notes .sidebar-copy,
    body.primary-shell .rail-coach-notes .activity-meta {
      color: rgba(235, 239, 226, 0.7);
      font-size: 0.62rem;
      line-height: 1.42;
    }

    body.primary-shell .rail-spark-trigger {
      position: relative;
      display: block;
      width: 100%;
      margin: 0;
      padding: 0;
      border: 0;
      border-radius: 3px;
      background: transparent;
      text-align: left;
      cursor: zoom-in;
    }

    body.primary-shell .rail-spark-trigger:hover {
      transform: none;
      background: rgba(218, 227, 211, 0.035);
    }

    body.primary-shell .rail-spark-trigger:focus-visible {
      outline: 1px solid rgba(218, 227, 211, 0.42);
      outline-offset: 4px;
    }

    body.primary-shell .rail-spark-expand {
      position: absolute;
      top: 2px;
      right: 3px;
      color: rgba(218, 227, 211, 0.56);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.42rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      opacity: 0;
      transition: opacity 120ms ease;
    }

    body.primary-shell .rail-spark-trigger:hover .rail-spark-expand,
    body.primary-shell .rail-spark-trigger:focus-visible .rail-spark-expand {
      opacity: 1;
    }

    body.primary-shell .coach-rail .day-spark .spark-elevation-fill {
      fill: rgba(208, 217, 194, 0.15);
    }

    body.primary-shell .coach-rail .day-spark .spark-elevation-line {
      stroke: rgba(220, 227, 207, 0.34);
      stroke-width: 1;
    }

    body.primary-shell .coach-rail .day-spark .spark-fill {
      fill: rgba(208, 217, 194, 0.08);
    }

    body.primary-shell .rail-spark-popover {
      width: min(780px, calc(100vw - 48px));
      margin: auto;
      padding: 18px 18px 14px;
      border: 1px solid rgba(23, 63, 49, 0.16);
      background: #f7f4ec;
      color: #1f2922;
      box-shadow: 0 26px 70px rgba(31, 39, 33, 0.2);
    }

    body.primary-shell .rail-spark-popover:popover-open {
      display: grid;
      gap: 16px;
    }

    body.primary-shell .rail-spark-popover::backdrop {
      background: rgba(25, 34, 29, 0.22);
      backdrop-filter: blur(2px);
    }

    body.primary-shell .rail-spark-popover-head {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 18px;
    }

    body.primary-shell .rail-spark-popover-head h3 {
      margin: 4px 0 0;
      color: #1c261f;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.55rem;
      font-weight: 500;
      letter-spacing: -0.045em;
    }

    body.primary-shell .rail-spark-popover-head .eyebrow {
      margin: 0;
    }

    body.primary-shell .rail-spark-popover-close {
      color: #556255;
      border: 1px solid rgba(23, 63, 49, 0.15);
      background: transparent;
      padding: 6px 10px;
      font-size: 0.68rem;
    }

    body.primary-shell .rail-spark-popover .expanded-spark {
      width: 100%;
      max-width: none;
      height: 214px;
    }

    body.primary-shell .rail-spark-popover .expanded-spark svg {
      height: 100%;
    }

    body.primary-shell .rail-spark-popover .day-spark .spark-elevation-fill {
      fill: rgba(73, 109, 69, 0.09);
    }

    body.primary-shell .rail-spark-popover .day-spark .spark-elevation-line {
      stroke: rgba(73, 109, 69, 0.28);
    }

    body.primary-shell .rail-spark-caption {
      margin: 0;
      color: #687366;
      font-size: 0.68rem;
      line-height: 1.4;
    }

    body.primary-shell .coach-rail .day-spark.bars {
      grid-template-rows: auto 34px;
      align-items: stretch;
      gap: 5px;
      height: 54px;
      margin-top: 9px;
      padding: 0 2px;
      overflow: visible;
    }

    body.primary-shell .coach-rail .day-spark.bars .spark-tag {
      position: static;
      justify-self: start;
      color: rgba(218, 227, 211, 0.58);
      font-size: 0.43rem;
      letter-spacing: 0.15em;
    }

    body.primary-shell .coach-rail .day-spark.bars .spark-bars {
      position: relative;
      height: 34px;
      gap: 2px;
      padding-bottom: 3px;
      border-bottom: 1px solid rgba(229, 235, 219, 0.18);
      overflow: visible;
    }

    body.primary-shell .coach-rail .day-spark.bars .spark-bars::before {
      content: "";
      position: absolute;
      inset: auto 0 14px;
      height: 1px;
      background: rgba(229, 235, 219, 0.08);
    }

    body.primary-shell .coach-rail .day-spark.bars i {
      position: relative;
      z-index: 1;
      min-height: 12px;
    }

    body.primary-shell .coach-rail textarea {
      border-color: rgba(255, 255, 255, 0.12);
      background: rgba(255, 255, 255, 0.04);
      color: #fff9ed;
    }

    body.primary-shell .coach-rail textarea::placeholder {
      color: rgba(255, 255, 255, 0.42);
    }

    body.primary-shell .coach-presence {
      border-top: 1px solid rgba(255, 255, 255, 0.16);
      background: var(--coach-spine-deep);
      padding: 21px 20px 23px;
    }

    body.primary-shell .coach-presence {
      margin-top: auto;
    }

    body.primary-shell .coach-presence {
      display: none;
    }

    body.primary-shell .coach-presence::before {
      display: none;
    }

    body.primary-shell .coach-presence .eyebrow,
    body.primary-shell .coach-presence .coach-prompt {
      color: rgba(214, 224, 206, 0.66);
    }

    body.primary-shell .coach-presence h3 {
      color: #f8f5e8;
      font-size: 1.96rem;
      max-width: 9ch;
    }

    body.primary-shell .coach-presence-copy p {
      color: rgba(234, 238, 226, 0.7);
      line-height: 1.48;
    }

    body.primary-shell .coach-presence-dot {
      background: #b7c87d;
      box-shadow: 0 0 0 4px rgba(183, 200, 125, 0.12);
    }

    body.primary-shell .coach-prompt {
      border-top-color: rgba(255, 255, 255, 0.13);
      padding-top: 8px;
    }

    body.primary-shell .center-stage {
      background: var(--coach-canvas);
      padding: 92px 18px 18px;
    }

    body.primary-shell .month-strip-shell {
      display: grid;
      gap: 8px;
      padding: 9px 10px 8px;
      border: 1px solid rgba(23, 63, 49, 0.12);
      background: rgba(73, 109, 69, 0.025);
    }

    body.primary-shell .month-strip-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }

    body.primary-shell .month-strip-meta > span:first-child,
    body.primary-shell .month-strip-legend {
      color: #77786f;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.43rem;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    body.primary-shell .month-strip-legend {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 500;
      letter-spacing: 0.1em;
    }

    body.primary-shell .month-strip-legend span {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }

    body.primary-shell .month-strip-legend i {
      display: block;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--accent);
    }

    body.primary-shell .month-strip-legend .quality {
      background: var(--gold);
    }

    body.primary-shell .month-strip-legend .race {
      width: 9px;
      height: 9px;
      background: var(--warning);
    }

    body.primary-shell .month-strip-scroll {
      min-width: 0;
      overflow-x: auto;
      overflow-y: hidden;
      scrollbar-width: none;
    }

    body.primary-shell .month-strip-scroll::-webkit-scrollbar {
      display: none;
    }

    body.primary-shell .month-strip-days {
      grid-template-columns: repeat(var(--month-day-count, 31), minmax(0, 1fr));
      gap: 0;
      min-width: 0;
      padding-top: 0;
    }

    body.primary-shell .month-strip-days::before {
      top: auto;
      right: 11px;
      bottom: 11px;
      left: 11px;
    }

    body.primary-shell .month-strip-day {
      height: 41px;
      gap: 7px;
      padding: 3px 0 4px;
    }

    body.primary-shell .month-strip-day span {
      color: #73766b;
      font-size: 0.48rem;
    }

    body.primary-shell .month-strip-day.weekend {
      background: rgba(73, 109, 69, 0.032);
    }

    body.primary-shell .month-strip-day:hover {
      background: rgba(73, 109, 69, 0.07);
      transform: none;
    }

    body.primary-shell .month-strip-day.selected {
      background: rgba(73, 109, 69, 0.09);
      box-shadow: inset 0 0 0 1px rgba(73, 109, 69, 0.32);
    }

    body.primary-shell .month-strip-day.selected::after {
      bottom: 1px;
      width: 18px;
    }

    @media (max-width: 640px) {
      body.primary-shell .month-strip-shell {
        padding-right: 8px;
        padding-left: 8px;
      }

      body.primary-shell .month-strip-meta {
        align-items: flex-start;
        flex-direction: column;
        gap: 7px;
      }

      body.primary-shell .month-strip-days {
        min-width: 730px;
      }
    }

    body.primary-shell .ride-sidebar {
      display: none !important;
    }

    body[data-view="weeks"] .toolbar {
      min-height: 38px;
      border: 0;
      background: transparent;
      padding: 8px 0 6px;
    }

    body[data-view="weeks"] .toolbar,
    body[data-view="weeks"] .toolbar select,
    body[data-view="weeks"] .toolbar button {
      color: #353c35;
    }

    body[data-view="weeks"] .toolbar select {
      appearance: none;
      border: 0;
      background: transparent;
      font-size: .68rem;
      color: #5a625a;
      padding-right: 18px;
      max-width: 240px;
    }

    body[data-view="weeks"] .toolbar label::after {
      content: "⌄";
      color: #6d776e;
      font-size: .7rem;
      margin-left: -12px;
      pointer-events: none;
    }

    body.primary-shell .topbar-actions .icon-button {
      display: inline-grid;
      place-items: center;
      padding: 0;
      line-height: 0;
    }

    body[data-view="weeks"] .week-select-fallback {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      white-space: nowrap;
      border: 0;
    }

    .week-range-label {
      min-width: 0;
      color: #5a625a;
      font-size: .68rem;
      white-space: nowrap;
    }

    .season-horizon {
      display: grid;
      gap: 4px;
      padding: 16px 22px 9px;
      border-bottom: 1px solid rgba(23, 63, 49, 0.14);
      background: var(--coach-paper-soft);
      cursor: default;
    }

    .season-horizon-head {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 24px;
    }

    .season-horizon-head strong {
      display: block;
      margin-top: 3px;
      color: #1a342a;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.05rem;
      font-weight: 500;
      letter-spacing: -0.04em;
    }

    .season-horizon-races {
      display: flex;
      align-items: flex-start;
      gap: 18px;
      padding-top: 2px;
    }

    .season-race {
      display: grid;
      gap: 2px;
      min-width: 92px;
      border: 0;
      border-left: 1px solid rgba(138, 76, 53, 0.45);
      border-radius: 0;
      background: transparent;
      padding: 0 0 0 8px;
      text-align: left;
    }

    .season-race span {
      color: #9a6b58;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.44rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }

    .season-race strong {
      margin: 0;
      color: #27342c;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 0.78rem;
      letter-spacing: -0.03em;
      line-height: 1.05;
    }

    .season-track-wrap {
      display: grid;
      gap: 7px;
    }

    .season-track-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 5px 14px;
      margin-top: 8px;
      color: #6e7569;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: .48rem;
      letter-spacing: .06em;
    }

    .season-chart-key {
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 5px 12px;
    }

    .season-chart-key strong { color: #344e40; font: inherit; font-weight: 700; }
    .season-chart-key span { display: inline-flex; align-items: center; gap: 5px; }
    .season-chart-key i { width: 13px; height: 7px; background: #c7d4c1; border: 1px solid #a6b99e; }
    .season-chart-key .trajectory-key i { height: 0; border: 0; border-top: 2px solid #789570; background: transparent; }
    .season-chart-key .recorded-key i { background: #597c65; border-color: #3e6550; }

    .season-selection-key {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: #925235;
    }

    .season-selection-key i {
      width: 9px;
      height: 9px;
      border: 1px solid #ae633f;
      background: rgba(174, 99, 63, .22);
    }

    .season-track-actions {
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 5px 12px;
    }

    .season-today-button {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: 0;
      border-radius: 3px;
      padding: 3px 5px;
      background: transparent;
      color: #365b49;
      font: inherit;
      letter-spacing: inherit;
      white-space: nowrap;
      cursor: pointer;
    }

    .season-today-button i {
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: currentColor;
    }

    .season-today-button:hover { background: rgba(23, 63, 49, .07); }
    .season-today-button:focus-visible { outline: 2px solid #173f31; outline-offset: 2px; }
    .season-today-button:disabled { opacity: .5; cursor: default; }

    .season-track {
      position: relative;
      height: 130px;
      cursor: pointer;
    }

    .season-track::before {
      content: "";
      position: absolute;
      inset: auto 0 0;
      height: 7px;
      border: 1px solid rgba(23, 63, 49, .14);
      border-radius: 2px;
      background: #edf0e9;
      pointer-events: none;
    }

    .season-load-chart {
      position: absolute;
      inset: 0 0 15px;
      width: 100%;
      height: calc(100% - 15px);
      overflow: visible;
    }

    .season-chart-grid { stroke: rgba(23, 63, 49, .13); stroke-width: 1; vector-effect: non-scaling-stroke; }
    .season-chart-grid.mid { stroke-dasharray: 2 5; }
    .season-planned-area { fill: #d4dfcd; fill-opacity: .36; }
    .season-target-band { fill: #c7d4c1; fill-opacity: .82; }
    .season-target-line { fill: none; stroke: #789570; stroke-width: 1.6; vector-effect: non-scaling-stroke; }
    .season-recorded-area { fill: #597c65; fill-opacity: .63; }
    .season-recorded-line { fill: none; stroke: #315a45; stroke-width: 1.4; vector-effect: non-scaling-stroke; }
    .season-week-hit { fill: transparent; }

    .season-chart-scale {
      position: absolute;
      inset: 0 2px 15px auto;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      align-items: flex-end;
      color: #788174;
      font: .45rem "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      pointer-events: none;
    }

    .season-chart-scale span { padding: 1px 3px; background: rgba(250, 250, 245, .83); }
    .season-chart-empty { position: absolute; inset: 0 0 15px; display: grid; place-items: center; color: #777e74; font-size: .7rem; pointer-events: none; }
    .season-load-readout { margin: -1px 0 0; color: #667265; font: .53rem "SFMono-Regular", Consolas, "Liberation Mono", monospace; line-height: 1.5; }

    .season-toolbar { flex-direction: row; align-items: center; flex-wrap: wrap; padding: 14px 24px; }
    .season-toolbar > div { width: auto; }
    .season-toolbar h2 { font-size: .65rem; }
    .season-toolbar .meta { margin: 5px 0 0; font-size: .62rem; letter-spacing: .025em; text-transform: none; }
    .season-toolbar .toolbar-actions { width: auto; margin-left: auto; }
    .season-toolbar select { width: auto; min-width: 96px; }
    .season-overview-horizon { gap: 12px; padding: 22px 24px 18px; }
    .season-overview-horizon .season-horizon-head > div:first-child > strong { font-size: 1.55rem; }
    .season-overview-horizon .season-horizon-races { flex-wrap: wrap; }
    .season-overview-horizon .season-horizon-races .season-race { max-width: 190px; }
    .season-overview-horizon .season-track { height: 270px; }
    .season-overview-horizon .season-track-meta { font-size: .58rem; margin-top: 0; }
    .season-overview-horizon .season-load-readout { font-size: .64rem; }
    .season-overview-horizon .season-chart-scale { font-size: .53rem; }
    .season-overview-copy { max-width: 760px; margin: 7px 0 0; color: #697366; font-size: .72rem; line-height: 1.55; }
    .season-overview-stats { display: flex; flex-wrap: wrap; gap: 7px 20px; margin: 0; color: #687365; font: .56rem "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
    .season-overview-stats strong { color: #294735; font-weight: 600; }
    .season-open-week { border: 1px solid rgba(23,63,49,.22); border-radius: 3px; padding: 5px 8px; background: transparent; color: #294735; font: inherit; cursor: pointer; }
    .season-open-week:disabled { opacity: .5; cursor: default; }
    .season-open-week:focus-visible { outline: 2px solid #173f31; outline-offset: 2px; }
    .season-event-track { position: relative; min-height: 23px; margin: -4px 0 -3px; border-top: 1px solid rgba(23,63,49,.12); }
    .season-event-marker { position: absolute; top: calc(var(--event-row, 0) * 18px); transform: translateX(-50%); display: grid; place-items: start center; width: 16px; height: 19px; padding: 0; border: 0; border-radius: 2px; background: transparent; color: #9a6b58; cursor: pointer; }
    .season-event-marker::before { content: ""; width: 1px; height: 8px; background: currentColor; }
    .season-event-marker::after { content: ""; position: absolute; top: 7px; width: 6px; height: 6px; transform: rotate(45deg); background: currentColor; }
    .season-event-marker.tentative { color: #9b9c87; }
    .season-event-marker:focus-visible { outline: 2px solid #173f31; outline-offset: 2px; z-index: 5; }
    .season-event-list { color: #667265; font-size: .63rem; line-height: 1.5; }
    .season-event-list summary { width: fit-content; cursor: pointer; }
    .season-event-list > div { display: flex; flex-wrap: wrap; gap: 8px 20px; padding-top: 10px; }
    .season-event-list .season-race { max-width: 240px; min-width: 0; }

    .season-track:focus-visible {
      outline: 2px solid #173f31;
      outline-offset: 3px;
    }

    .season-phase {
      position: absolute;
      bottom: 1px;
      height: 5px;
      min-width: 0;
      overflow: hidden;
    }

    .season-phase.base { background: #d9e2d7; }
    .season-phase.build { background: #b5c5ad; }
    .season-phase.race { background: #9eb299; }
    .season-phase.recover { background: #e4e2d7; }

    .season-selected-range {
      position: absolute;
      inset-block: 0;
      z-index: 2;
      min-width: 2px;
      border: 1px solid #ae633f;
      background: rgba(174, 99, 63, .12);
      pointer-events: none;
    }

    .season-day-marker {
      position: absolute;
      inset-block: 0;
      z-index: 3;
      width: 1px;
      transform: translateX(-50%);
      background: #173f31;
      pointer-events: none;
    }

    .season-today-marker {
      position: absolute;
      inset-block: 0;
      z-index: 4;
      width: 0;
      border-left: 1px dashed #365b49;
      pointer-events: none;
    }

    .season-today-marker::before {
      content: "";
      position: absolute;
      top: -1px;
      left: -3px;
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: #365b49;
    }

    .season-months {
      position: relative;
      height: 12px;
      color: #817c6f;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.43rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }

    .season-months span {
      position: absolute;
      top: 0;
      overflow: hidden;
      padding-left: 2px;
      white-space: nowrap;
    }

    .week-load-overview {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-block: 1px solid var(--rule);
      background: #f1f3ec;
    }

    .week-load-overview > div { min-width: 0; padding: 12px 18px; border-right: 1px solid var(--rule); }
    .week-load-overview > div:last-child { border-right: 0; }
    .week-load-overview span { display: block; color: #6e786b; font: .49rem "SFMono-Regular", Consolas, "Liberation Mono", monospace; letter-spacing: .08em; text-transform: uppercase; }
    .week-load-overview strong { display: block; margin-top: 5px; color: #223d2e; font: 500 1.18rem/1.15 ui-serif, Georgia, serif; letter-spacing: -.035em; overflow-wrap: anywhere; }
    .week-load-overview small { display: block; margin-top: 4px; color: #70786c; font-size: .58rem; line-height: 1.35; }
    .week-load-overview .forecast-value small { color: #925235; }
    .week-plan-summary { margin-bottom: 8px; padding-bottom: 7px; border-bottom: 1px solid rgba(23,63,49,.1); }
    .week-stats-caption { display: block; margin-bottom: 3px; color: #7b8276; font: .43rem "SFMono-Regular", Consolas, "Liberation Mono", monospace; letter-spacing: .08em; text-transform: uppercase; }
    .week-plan-values { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 3px 8px; color: #556b56; font-size: .64rem; font-variant-numeric: tabular-nums; }
    .week-plan-values strong { font-weight: 500; }
    .week-load-note { margin: 3px 0 7px; color: #8c674f; font-size: .53rem; line-height: 1.35; overflow-wrap: anywhere; }
    .week-budget-note { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--rule); color: var(--muted); font-size: .68rem; line-height: 1.5; overflow-wrap: anywhere; }
    .week-budget-note p { margin: 0 0 7px; }
    .week-budget-note ul { margin: 5px 0 0; padding-left: 18px; }
    .week-budget-note strong { color: var(--ink); }
    .week-structured-plan { margin-top: 7px; padding-top: 6px; border-top: 1px solid rgba(23,63,49,.1); color: #627362; font-size: .58rem; line-height: 1.4; overflow-wrap: anywhere; }
    .rail-load-note { margin: 7px 0 0; color: rgba(229, 228, 207, .76); font-size: .62rem; line-height: 1.45; }

    @media (max-width: 760px) {
      .week-load-overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .week-load-overview > div { padding: 10px 12px; }
      .week-load-overview > div:nth-child(2) { border-right: 0; }
      .week-load-overview > div:nth-child(-n+2) { border-bottom: 1px solid var(--rule); }
      .season-track { height: 112px; }
      .season-overview-horizon { padding: 16px 14px; }
      .season-overview-horizon .season-track { height: 200px; }
      .season-overview-horizon .season-horizon-head { flex-wrap: wrap; gap: 12px; }
      .season-overview-horizon .season-horizon-races { flex-wrap: wrap; gap: 12px; }
    }

    body[data-view="weeks"] .week-desk {
      display: none;
    }

    .week-intel {
      display: grid;
      grid-template-columns: minmax(0, 1.58fr) minmax(190px, .58fr) minmax(215px, .62fr);
      gap: 0;
      border-bottom: 1px solid var(--rule);
      background: var(--coach-paper);
    }

    .week-thesis,
    .week-status,
    .week-intel .week-stance {
      min-width: 0;
      padding: 20px 22px 18px;
      border-right: 1px solid var(--rule);
    }

    .week-intel .week-stance {
      border-right: 0;
      background: var(--coach-paper-soft);
      display: grid;
      align-content: space-between;
    }

    .week-status,
    .week-health {
      display: grid;
      align-content: start;
      gap: 9px;
    }

    .week-status-summary {
      display: grid;
      gap: 9px;
      cursor: pointer;
      list-style: none;
    }

    .week-status-summary::-webkit-details-marker {
      display: none;
    }

    .week-status-summary:focus-visible {
      outline: 2px solid #476e51;
      outline-offset: 5px;
    }

    .week-status-main {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 4px;
    }

    .week-status-main strong {
      color: #19271f;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 2rem;
      font-weight: 500;
      letter-spacing: -.05em;
      line-height: 1;
    }

    .week-status-disclosure {
      width: 8px;
      height: 8px;
      flex: 0 0 auto;
      border-right: 1px solid #476e51;
      border-bottom: 1px solid #476e51;
      transform: rotate(45deg) translateY(-2px);
      transition: transform 160ms ease;
    }

    details.week-status[open] .week-status-disclosure {
      transform: rotate(225deg) translate(-2px, -2px);
    }

    .week-status-copy {
      display: block;
      color: #5a5b53;
      font-size: .69rem;
      line-height: 1.48;
    }

    .week-status-details {
      margin-top: 14px;
      padding-top: 2px;
      border-top: 1px solid var(--rule);
    }

    .week-status-metrics {
      display: grid;
    }

    .week-status-metric {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: baseline;
      padding: 8px 0;
      border-bottom: 1px solid var(--rule);
    }

    .week-status-metric span {
      color: #7a766c;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: .46rem;
      letter-spacing: .08em;
      text-transform: uppercase;
    }

    .week-status-metric strong {
      color: #263626;
      font-size: .68rem;
      font-weight: 650;
      text-align: right;
    }

    .week-status-main i,
    .week-health-note i {
      display: inline-grid;
      place-items: center;
      width: 26px;
      height: 26px;
      border: 1px solid #476e51;
      border-radius: 999px;
      color: #315a3c;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: .9rem;
      font-style: normal;
    }

    .week-status p,
    .week-health-note span {
      margin: 0;
      color: #5a5b53;
      font-size: .69rem;
      line-height: 1.48;
    }

    .week-status-events {
      display: grid;
      gap: 7px;
      margin-top: 5px;
      padding-top: 10px;
      border-top: 1px solid var(--rule);
    }

    .week-status-events .event-row {
      display: grid;
      gap: 4px;
      padding: 0;
      background: transparent;
    }

    .week-status-events .event-chip {
      width: fit-content;
      max-width: 100%;
      padding: 4px 6px;
      font-size: .41rem;
      line-height: 1.18;
      white-space: normal;
    }

    .week-status button,
    .week-read-more {
      width: fit-content;
      border: 0;
      background: transparent;
      padding: 0;
      color: #2f6243;
      font-size: .66rem;
    }

    .week-health {
      gap: 8px;
      padding-top: 22px;
    }

    .week-health-row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 8px;
      align-items: center;
      color: #444a42;
      font-size: .66rem;
    }

    .week-health-row strong {
      color: #3f5142;
      font-size: .63rem;
      font-weight: 500;
    }

    .dots {
      display: inline-block;
      width: 34px;
      height: 6px;
      background:
        radial-gradient(circle at 3px 3px, #2d6843 0 2px, transparent 2.2px),
        radial-gradient(circle at 11px 3px, #2d6843 0 2px, transparent 2.2px),
        radial-gradient(circle at 19px 3px, #2d6843 0 2px, transparent 2.2px),
        radial-gradient(circle at 27px 3px, rgba(45,104,67,.25) 0 2px, transparent 2.2px),
        radial-gradient(circle at 35px 3px, rgba(45,104,67,.25) 0 2px, transparent 2.2px);
    }

    .dots.steady {
      opacity: .76;
    }

    .dots.rising {
      opacity: .64;
    }

    .week-health-note {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      margin-top: 8px;
      padding-top: 10px;
      border-top: 1px solid var(--rule);
    }

    .week-health-note i {
      width: 22px;
      height: 22px;
      font-size: .74rem;
      flex: 0 0 auto;
    }

    .week-intel .week-stance strong {
      font-size: 1.33rem;
      line-height: 1.04;
    }

    .week-intel .week-stance p {
      max-width: 23ch;
      font-size: .67rem;
      line-height: 1.48;
    }

    .week-read-more {
      align-self: end;
      margin-top: 14px;
    }

    .week-overview {
      display: none;
    }

    .rider-state,
    .rider-profile {
      display: none;
    }

    body[data-view="weeks"] .rider-state {
      display: none;
      gap: 6px;
      padding: 14px 20px 16px;
      border-top: 1px solid rgba(255,255,255,.16);
      background: #123226;
    }

    .rider-state .eyebrow {
      margin-bottom: 2px;
      color: rgba(214,224,206,.66);
    }

    .rider-state-row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      align-items: center;
      gap: 8px;
      color: rgba(239,245,233,.72);
      font-size: .64rem;
    }

    .rider-state-row strong,
    .rider-state-row em {
      color: rgba(244,248,240,.72);
      font-size: .61rem;
      font-style: normal;
      font-weight: 400;
    }

    body[data-view="weeks"] .rider-profile {
      display: none;
      align-items: center;
      gap: 10px;
      padding: 16px 20px 18px;
      border-top: 1px solid rgba(255,255,255,.16);
      background: #123226;
    }

    .rider-avatar {
      display: grid;
      place-items: center;
      width: 32px;
      height: 32px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      color: #f5f7ef;
      font-size: .63rem;
    }

    .rider-profile strong,
    .rider-profile span {
      display: block;
    }

    .rider-profile strong {
      color: #f4f7ef;
      font-size: .74rem;
      font-weight: 500;
    }

    .rider-profile span {
      color: rgba(239,245,233,.58);
      font-size: .58rem;
    }

    body[data-view="weeks"] .week-desk-main {
      padding: 16px 24px 15px 20px;
    }

    body[data-view="weeks"] .week-title {
      max-width: 900px;
      font-size: 2.35rem;
      line-height: 1.01;
      letter-spacing: -0.066em;
    }

    body[data-view="weeks"] .week-focus {
      max-width: 700px;
      margin-top: 8px;
      font-size: 0.73rem;
    }

    body[data-view="weeks"] .week-stance {
      padding: 16px 18px 14px;
      background: var(--coach-paper-soft);
    }

    body[data-view="weeks"] .week-stance strong {
      font-size: 1.18rem;
    }

    body[data-view="weeks"] .week-stance p {
      font-size: 0.61rem;
    }

    body[data-view="weeks"] .week-overview {
      padding: 0 14px;
      background: var(--coach-paper);
    }

    body[data-view="weeks"] .week-days {
      padding: 7px 14px 0;
      background: var(--coach-paper);
      grid-template-columns: repeat(7, minmax(0, 1fr));
    }

    body[data-view="weeks"] .week-day {
      height: auto;
      min-height: 314px;
      padding-top: 10px;
      padding-right: 12px;
      padding-left: 12px;
      overflow: visible;
    }

    body[data-view="weeks"] .week-day .date-label {
      letter-spacing: .11em;
    }

    body[data-view="weeks"] .week-day .actual {
      margin-bottom: 3px;
      line-height: 1.06;
      font-size: .98rem;
    }

    body[data-view="weeks"] .week-day .planned {
      line-height: 1.28;
      font-size: .58rem;
    }

    body[data-view="weeks"] .week-day .actual,
    body[data-view="weeks"] .week-day .planned {
      display: block;
      overflow: visible;
      text-overflow: clip;
      white-space: normal;
      -webkit-line-clamp: unset;
    }

    body[data-view="weeks"] .week-day:nth-child(7) .actual {
      font-size: .9rem;
      line-height: 1.08;
    }

    body[data-view="weeks"] .week-day .event-row {
      display: grid;
      gap: 4px;
      padding: 0;
      margin-top: 6px;
      background: transparent;
    }

    body[data-view="weeks"] .week-day .event-chip {
      display: block;
      width: fit-content;
      max-width: 100%;
      padding: 4px 6px;
      border-radius: 999px;
      font-size: .39rem;
      line-height: 1.1;
      white-space: normal;
    }

    body[data-view="weeks"] .week-day-footer {
      margin-top: auto;
    }

    body[data-view="weeks"] .week-day-cue,
    body[data-view="weeks"] .ride-cue {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      margin-top: 10px;
      padding-top: 9px;
      border-top: 1px solid rgba(23,63,49,.1);
    }

    body[data-view="weeks"] .week-day-cue span {
      width: 5px;
      height: 5px;
      margin-top: 4px;
      border-radius: 999px;
      background: #527c4a;
      flex: 0 0 auto;
    }

    body[data-view="weeks"] .week-day-cue p,
    body[data-view="weeks"] .ride-cue {
      margin: 0;
      color: #5a6456;
      font-size: .57rem;
      line-height: 1.32;
    }

    body[data-view="weeks"] .week-stat-chip-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }

    body[data-view="weeks"] .week-load-chart {
      padding-top: 9px;
      padding-bottom: 12px;
    }

    body[data-view="weeks"] .week-load-chart svg {
      height: 94px;
    }

    body[data-view="weeks"] .week-load-chart h4 {
      font-size: .94rem;
    }

    body[data-view="weeks"] .chart-legend {
      font-size: .44rem;
    }

    body[data-view="weeks"] .month-rail {
      display: none;
    }

    body[data-view="weeks"] .week-load-chart {
      margin: 0 14px 14px;
    }

    @media (max-width: 1240px) {
      .week-intel {
        grid-template-columns: minmax(0, 1.42fr) minmax(174px, .54fr) minmax(184px, .54fr);
      }

      body[data-view="weeks"] .week-title {
        font-size: 2rem;
        line-height: 1.01;
      }

      .week-thesis,
      .week-status,
      .week-intel .week-stance {
        padding-right: 18px;
        padding-left: 18px;
      }

      .week-status-main strong {
        font-size: 1.8rem;
      }

      .week-intel .week-stance strong {
        font-size: 1.14rem;
      }

      .week-intel .week-stance p,
      .week-status p {
        font-size: .62rem;
      }
    }

    @media (max-width: 1180px) {
      .week-desk {
        grid-template-columns: 1fr;
      }

      .week-stance {
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: end;
        gap: 20px;
        border-top: 1px solid var(--rule);
        border-left: 0;
      }

      .week-stance-foot {
        min-width: 170px;
        margin-top: 0;
      }
    }

    @media (max-width: 860px) {
      .week-desk-main {
        padding: 18px 16px 17px;
      }

      .week-title {
        font-size: 2.55rem;
      }

      .week-stance {
        grid-template-columns: 1fr;
        padding: 16px;
      }

      .summary-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .summary-card:nth-child(3) {
        border-right: 0;
      }

      .summary-card:nth-child(n+4) {
        border-top: 1px solid var(--rule);
      }

      .week-progress-band {
        grid-template-columns: 1fr;
      }
    }

    body[data-view="today"] .today-dashboard {
      grid-template-columns: minmax(0, 1.08fr) minmax(290px, 0.92fr);
    }

    body[data-view="today"] .today-card {
      min-height: 560px;
    }

    body[data-view="today"] .today-card h3 {
      max-width: 760px;
      font-size: clamp(2.6rem, 3.8vw, 4.25rem);
      line-height: 0.95;
    }

    body[data-view="today"] .today-card.primary {
      align-content: start;
      padding-top: 24px;
    }

    body[data-view="today"] .today-plan-copy {
      max-width: 680px;
      margin-top: 2px;
      font-size: 0.9rem;
      line-height: 1.48;
    }

    body[data-view="today"] .today-load-card {
      min-height: 210px;
      padding-top: 16px;
    }

    @media (max-width: 960px) {
      body[data-view="today"] .workspace {
        grid-template-columns: 1fr;
      }

      body[data-view="today"] .today-dashboard {
        grid-template-columns: 1fr;
      }

      body.primary-shell .context-stack {
        display: none;
      }

      body.primary-shell .workspace {
        grid-template-columns: 1fr;
      }

      .ride-sidebar.open {
        position: fixed;
        inset: auto 10px 10px 10px;
        z-index: 60;
        display: grid;
        width: auto;
        max-height: 72vh;
        overflow: auto;
      }
    }

    @media (max-width: 1180px) {
      body[data-view="weeks"] .week-days {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }

      body[data-view="weeks"] .week-day:nth-child(4n) {
        border-right: 0;
      }

      body[data-view="weeks"] .week-day:nth-child(n+5) {
        border-top: 1px solid var(--rule);
      }
    }

    @media (max-width: 960px) {
      body.primary-shell .topbar {
        width: 100%;
        margin-left: 0;
      }

      body.primary-shell .topbar .brand {
        visibility: visible;
      }

      body.primary-shell .brand-mark {
        color: var(--coach-spine);
        font-size: 1.22rem;
        font-style: normal;
        font-weight: 500;
        letter-spacing: -0.04em;
        text-transform: uppercase;
      }

      body.primary-shell .workspace {
        margin-top: 0;
      }

      body.primary-shell .center-stage {
        padding: 0;
      }

      body[data-view="weeks"] .week-intel {
        grid-template-columns: 1fr;
      }

      body[data-view="weeks"] .week-thesis,
      body[data-view="weeks"] .week-status,
      body[data-view="weeks"] .week-intel .week-stance {
        border-right: 0;
        border-bottom: 1px solid var(--rule);
        padding: 16px;
      }

      body[data-view="weeks"] .week-intel .week-stance {
        border-bottom: 0;
      }

      body[data-view="weeks"] .week-title {
        max-width: none;
        font-size: 2rem;
      }

      body[data-view="weeks"] .season-horizon {
        padding-inline: 16px;
      }

      body[data-view="weeks"] .season-horizon-head {
        display: grid;
        gap: 12px;
      }

      body[data-view="weeks"] .season-horizon-races {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }

      body[data-view="weeks"] .season-race {
        min-width: 0;
      }

      body[data-view="weeks"] .week-days {
        grid-template-columns: 1fr;
        padding-inline: 0;
      }

      body[data-view="weeks"] .week-day,
      body[data-view="weeks"] .week-day:nth-child(4n) {
        min-height: 0;
        height: auto;
        border-top: 1px solid var(--rule);
        border-right: 0;
        border-left: 0;
      }

      body[data-view="weeks"] .week-day:first-child {
        border-top: 2px solid transparent;
      }
    }
    .plan-export-dialog {
      width: min(560px, calc(100vw - 32px));
      border: 1px solid var(--line-strong);
      border-radius: 18px;
      padding: 24px;
      color: var(--text);
      background: #202820;
      box-shadow: var(--shadow);
    }
    .plan-export-dialog::backdrop { background: rgba(0, 0, 0, 0.65); }
    .plan-export-dialog h2 { margin: 0 0 10px; }
    .plan-export-dialog p { color: var(--muted); }
    .plan-export-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 20px 0; }
    .plan-export-fields label { display: grid; gap: 7px; font-size: 0.88rem; }
    .plan-export-fields input, .plan-export-fields select { width: 100%; min-width: 0; padding: 10px; border: 1px solid var(--line-strong); border-radius: 8px; background: var(--bg); color: var(--text); }
    .plan-export-format { grid-column: 1 / -1; }
    .plan-export-actions { display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; }
    .plan-export-status { min-height: 1.5em; overflow-wrap: anywhere; }
  </style>
</head>
<body class="primary-shell" data-view="weeks">
  <div id="recording-drop-overlay" class="recording-drop-overlay" hidden aria-hidden="true">
    <div class="recording-drop-card">
      <strong>Drop ride files to import</strong>
      <span>FIT, TCX, or GPX. Files stay in this local athlete workspace.</span>
    </div>
  </div>
  <input id="activity-recording-input" type="file" accept=".fit,.tcx,.gpx" multiple hidden />
  <main class="page">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">GRADIENT ASCENT</span>
        <p class="brand-kicker">Local Training Center</p>
      </div>
      <nav class="tabs" aria-label="Training center views">
        <button class="tab today-tab" type="button" data-view="today" title="Jump to today">
          <span>Today</span>
        </button>
        <button class="tab active" type="button" data-view="weeks">Week</button>
        <button class="tab" type="button" data-view="calendar">Season</button>
        <button class="tab" type="button" data-view="progress">Progress</button>
      </nav>
      <div class="topbar-actions">
        <a id="ask-coach-button" class="coach-conversation-button" href="#" aria-label="Start a coaching conversation in Codex">Ask Coach</a>
        <button id="sync-button" class="icon-button sync-button" type="button" aria-label="Refresh data" title="Refresh data">
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M21 12a9 9 0 1 1-2.64-6.36"></path>
            <path d="M21 3v6h-6"></path>
          </svg>
        </button>
        <div class="action-menu-wrap">
          <button id="more-actions-button" class="icon-button" type="button" aria-label="More actions" aria-haspopup="menu" aria-expanded="false" title="More actions">
            <svg aria-hidden="true" viewBox="0 0 24 24">
              <path d="M12 12h.01"></path>
              <path d="M19 12h.01"></path>
              <path d="M5 12h.01"></path>
            </svg>
          </button>
          <div id="more-actions-menu" class="action-menu" role="menu" hidden>
            <button id="import-ride-file" type="button" role="menuitem">Import ride file…</button>
            <button id="export-all-xlsx" type="button" role="menuitem">Download</button>
            <button id="export-planned-schedule" type="button" role="menuitem">Export planned schedule…</button>
            <button id="open-connections" type="button" role="menuitem">Connections</button>
            <button id="open-settings" type="button" role="menuitem">Settings</button>
          </div>
        </div>
        <span id="status-text" class="sr-status" role="status" aria-live="polite"></span>
      </div>
    </header>

    <section class="workspace">
      <div class="context-stack">
        <div class="sidebar-brand">
          <strong>GRADIENT ASCENT</strong>
          <span>Local Training Center</span>
        </div>
        <aside id="coach-rail" class="coach-rail panel" aria-label="Selected day coaching dashboard">
          <div class="coach-rail-head">
            <div>
              <p id="coach-day-context-label" class="eyebrow">Today</p>
              <h2 id="coach-date-label">Select a day</h2>
            </div>
            <div class="day-nav">
              <button id="previous-day" class="icon-button" type="button" aria-label="Previous day" title="Previous day">
                <svg aria-hidden="true" viewBox="0 0 24 24">
                  <path d="m15 18-6-6 6-6"></path>
                </svg>
              </button>
              <button id="jump-to-today" class="icon-button today-return" type="button" aria-label="Jump to today" title="Jump to today">
                <svg aria-hidden="true" viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="7"></circle>
                  <circle class="target-dot" cx="12" cy="12" r="2"></circle>
                </svg>
              </button>
              <button id="next-day" class="icon-button" type="button" aria-label="Next day" title="Next day">
                <svg aria-hidden="true" viewBox="0 0 24 24">
                  <path d="m9 18 6-6-6-6"></path>
                </svg>
              </button>
            </div>
          </div>
          <div id="coach-rail-content" class="coach-rail-content"></div>
        </aside>

        <aside class="coach-presence panel" aria-label="Coach presence">
          <div class="coach-presence-head">
            <div>
              <p id="coach-presence-eyebrow" class="eyebrow">Coach read</p>
              <h3 id="coach-presence-title">Keep the shape.</h3>
            </div>
            <span class="coach-presence-dot" aria-hidden="true"></span>
          </div>
          <div class="coach-presence-copy">
            <p id="coach-presence-copy">Let the day serve the week, not the other way around.</p>
          </div>
          <div class="coach-prompt-list" aria-label="Suggested coach prompts">
            <button type="button" class="coach-prompt">Why this today?</button>
            <button type="button" class="coach-prompt">Is the week still right?</button>
            <button type="button" class="coach-prompt">How does this fit the build?</button>
          </div>
        </aside>

        <aside class="rider-state panel" aria-label="Rider state">
          <p class="eyebrow">Rider state</p>
          <p class="sidebar-copy">Recovery data unavailable</p>
        </aside>

        <aside class="rider-profile panel" aria-label="Rider profile">
          <div id="rider-avatar" class="rider-avatar">A</div>
          <div><strong id="rider-name">__RIDER_NAME__</strong><span id="rider-description">__RIDER_DESCRIPTION__</span></div>
        </aside>

        <aside id="ride-sidebar" class="ride-sidebar" aria-hidden="true" aria-label="Ride details">
          <div id="ride-sidebar-content" class="ride-sidebar-content"></div>
        </aside>
      </div>

      <div class="center-stage">
        <section id="today-view" class="view panel">
          <div class="toolbar">
            <div>
              <h2 id="today-view-label">Today</h2>
              <p id="today-meta" class="meta"></p>
            </div>
            <div class="toolbar-actions today-toolbar-actions">
              <input id="today-date-picker" type="date" aria-label="Select day" />
            </div>
          </div>
          <div id="today-dashboard" class="today-dashboard"></div>
        </section>

        <section id="calendar-view" class="view panel">
          <div class="toolbar season-toolbar">
            <div>
              <h2>Season</h2>
              <p id="calendar-meta" class="meta"></p>
            </div>
            <div class="toolbar-actions">
              <select id="calendar-year-select" aria-label="Select calendar year"></select>
            </div>
          </div>
          <div id="season-overview"></div>
          <div id="calendar-grid" class="calendar-grid"></div>
        </section>

        <section id="weeks-view" class="view panel active">
          <div class="week-controls-fallback" hidden>
            <button id="previous-week" type="button" aria-label="Previous week">Previous week</button>
            <select id="week-select" aria-label="Select week"></select>
            <button id="next-week" type="button" aria-label="Next week">Next week</button>
          </div>
          <div id="week-list" class="week-list"></div>
        </section>

        <section id="progress-view" class="view panel">
          <div class="toolbar">
            <div>
              <h2>Progress</h2>
              <p class="meta">Coach-generated progress view.</p>
            </div>
          </div>
          <iframe
            id="progress-frame"
            class="progress-frame"
            src="./progress.html"
            title="Coach-generated progress"
          ></iframe>
        </section>

        <section id="connections-view" class="view panel">
          <div class="toolbar">
            <div>
              <h2>Connections</h2>
              <p class="meta">Connect Ride with GPS, or drag FIT, TCX, or GPX files onto this page.</p>
            </div>
            <div class="toolbar-actions">
              <button id="connections-import-ride-file" type="button">Import ride file</button>
            </div>
          </div>
          <div id="connections-root" class="connection-layout" aria-live="polite"></div>
        </section>

        <section id="settings-view" class="view panel">
          <div class="toolbar">
            <div>
              <h2>Settings</h2>
              <p class="meta">Local dashboard preferences.</p>
            </div>
          </div>
          <div class="settings-layout">
            <label class="setting-row">
              <span>Default view</span>
              <select id="default-view-setting">
                <option value="today">Today</option>
                <option value="weeks">Week</option>
                <option value="calendar">Calendar</option>
              </select>
            </label>
            <label class="setting-toggle">
              <input id="ride-sidebar-setting" type="checkbox" />
              <span>Open ride details by default</span>
            </label>
          </div>
        </section>

        <section id="month-rail" class="month-rail panel" aria-label="Month context"></section>
      </div>
    </section>
  </main>

  <dialog id="plan-export-dialog" class="plan-export-dialog" aria-labelledby="plan-export-title">
    <form id="plan-export-form">
      <h2 id="plan-export-title">Export planned schedule</h2>
      <p>Download a calendar, spreadsheet, or an offline bundle with compatible Garmin/Wahoo FIT workout files. Only explicitly defined intervals become device workouts; plan descriptions stay calendar-only.</p>
      <div class="plan-export-fields">
        <label>From<input id="plan-export-start" type="date" /></label>
        <label>Through<input id="plan-export-end" type="date" /></label>
        <label class="plan-export-format">Download format<select id="plan-export-format">
          <option value="zip">Complete bundle (.zip)</option>
          <option value="ics">Calendar (.ics)</option>
          <option value="csv">Plan spreadsheet (.csv)</option>
        </select></label>
      </div>
      <p id="plan-export-status" class="plan-export-status" role="status" aria-live="polite">Nothing is uploaded or sent to a device automatically.</p>
      <div class="plan-export-actions">
        <button id="plan-export-all-dates" type="button">All dates</button>
        <button id="plan-export-close" type="button">Close</button>
        <button id="plan-export-download" class="primary" type="submit">Download plan</button>
      </div>
    </form>
  </dialog>

  <script src="__TRAINING_CENTER_DATA_SRC__"></script>
  <script>
    const DATA = window.__COACH_TRAINING_CENTER_DATA__ || { weeks: [], days: [], notes: {}, coachNotes: {} };
    const ACTIVITY_DETAILS = window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ ||
      (window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ = {});
    const DAY_BY_DATE = new Map((DATA.days || []).map((day) => [day.date, day]));
    const DAYS_BY_WEEK = new Map();
    for (const day of DATA.days || []) {
      if (!DAYS_BY_WEEK.has(day.week_start)) DAYS_BY_WEEK.set(day.week_start, []);
      DAYS_BY_WEEK.get(day.week_start).push(day);
    }
    const WEEK_BY_START = new Map((DATA.weeks || []).map((week) => [week.start_date, week]));
    for (const week of DATA.weeks || []) {
      // Keep one serialized copy of each day while preserving the existing in-memory contract.
      week.days = DAYS_BY_WEEK.get(week.start_date) || [];
      week.activityDetailsLoaded = false;
    }
    const ACTIVITY_DETAIL_LOADS = new Map();
    const COACH_CONVERSATION_PROMPT = "Use $coach-advice to review my training, recovery, goals, and upcoming plans, then tell me what to do next.";
    const NOTES_API = "./api/daily-notes";
    const SYNC_API = "./api/sync";
    const CONNECTIONS_API = "./api/connections";
    const RIDE_SETUP_API = "./api/connections/ridewithgps/setup";
    const PLAN_EXPORT_API = "./api/plan/export";
    const STRAVA_ARCHIVE_API = "./api/connections/strava/archive";
    const ACTIVITY_RECORDINGS_API = "./api/activity-recordings";
    const STRAVA_EXPORT_URL = "https://www.strava.com/athlete/download_my_account";
    const ATHLETE_TIME_ZONE = DATA.athlete?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    let TODAY = isoDateInTimeZone(new Date(), ATHLETE_TIME_ZONE);
    const MONTH_FORMAT = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
    const LONG_DATE_FORMAT = new Intl.DateTimeFormat(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
    const SHORT_DATE_FORMAT = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
    const UNIT_SYSTEM = DATA.athlete?.unit_system === "metric" ? "metric" : "imperial";
    const BIG_DAY_HOURS = 4;
    const BIG_DAY_TSS = 100;
    const BIG_DAY_LABEL = "4h+ or 100+ TSS";
    const POWER_ZONE_FTP = Number(DATA.athlete?.ftp_w);
    const VIEW_STORAGE_KEY = "coach-default-view";
    const RIDE_SIDEBAR_STORAGE_KEY = "coach-ride-sidebar-open";
    const FLASH_STATUS_STORAGE_KEY = "coach-flash-status";
    const PRIMARY_VIEWS = new Set(["today", "weeks", "calendar", "progress"]);
    const state = {
      view: initialView(),
      selectedDate: pickInitialDate(),
      calendarYear: null,
      selectedWeekStart: null,
      notes: { ...(DATA.notes || {}) },
      notesWritable: false,
      notesPath: "configured daily_notes.json",
      writeToken: "",
      noteSaveTimers: new Map(),
      syncPollTimer: null,
      rideSetup: null,
      rideSetupPollTimer: null,
      rideSidebarOpen: initialRideSidebarOpen(),
      connections: null
    };
    const requestedCalendarYear = new URLSearchParams(window.location.search).get("year");
    state.calendarYear = state.view === "calendar" && /^[0-9]{4}$/.test(requestedCalendarYear || "")
      ? requestedCalendarYear : state.selectedDate.slice(0, 4);
    state.selectedWeekStart = weekForDate(state.selectedDate)?.start_date || DATA.weeks[0]?.start_date || null;

    function initialView() {
      const requested = new URLSearchParams(window.location.search).get("view");
      if (requested === "season") return "calendar";
      if (["today", "weeks", "calendar", "progress", "connections", "settings"].includes(requested)) {
        return requested;
      }
      const stored = window.localStorage.getItem(VIEW_STORAGE_KEY);
      return ["today", "weeks", "calendar", "progress"].includes(stored) ? stored : "weeks";
    }

    function initialRideSidebarOpen() {
      return window.localStorage.getItem(RIDE_SIDEBAR_STORAGE_KEY) !== "false";
    }

    function pickInitialDate() {
      const requestedDate = new URLSearchParams(window.location.search).get("date");
      if (requestedDate && DATA.days?.some((day) => day.date === requestedDate)) return requestedDate;
      if (DATA.days?.some((day) => day.date === TODAY)) return TODAY;
      const lastSynced = DATA.postSyncSummary?.sources?.strava?.last;
      if (lastSynced && DATA.days?.some((day) => day.date === lastSynced)) return lastSynced;
      return todayAnchorDate();
    }

    function todayAnchorDate() {
      if (DATA.days?.some((day) => day.date === TODAY)) return TODAY;
      const past = (DATA.days || []).filter((day) => day.date < TODAY).at(-1);
      return past?.date || DATA.days?.[0]?.date || TODAY;
    }

    function todayAnchorLabel() {
      const anchor = todayAnchorDate();
      return anchor === TODAY ? "Today" : anchor < TODAY ? "Latest" : "Next";
    }

    function localIsoDate(value) {
      const shifted = new Date(value.valueOf() - value.getTimezoneOffset() * 60000);
      return shifted.toISOString().slice(0, 10);
    }

    function isoDateInTimeZone(value, timeZone) {
      try {
        const parts = new Intl.DateTimeFormat(undefined, {
          timeZone,
          year: "numeric",
          month: "2-digit",
          day: "2-digit"
        }).formatToParts(value);
        const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
        if (values.year && values.month && values.day) {
          return `${values.year}-${values.month}-${values.day}`;
        }
      } catch (_error) {
        // Invalid legacy profile timezones fall back to the browser's local date.
      }
      return localIsoDate(value);
    }

    function refreshCurrentDate() {
      const today = isoDateInTimeZone(new Date(), ATHLETE_TIME_ZONE);
      if (today === TODAY) return false;
      TODAY = today;
      renderTodayTabLabel();
      renderCalendar();
      renderWeek();
      renderCoachRail();
      renderTodayDashboard();
      renderMonthRail();
      renderRideSidebar();
      return true;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function truncate(value, length = 82) {
      const text = String(value || "").replace(/\\s+/g, " ").trim();
      if (text.length <= length) return text;
      return `${text.slice(0, length - 3)}...`;
    }

    function renderAthleteProfile() {
      const athlete = DATA.athlete || {};
      const configuredName = String(athlete.display_name || "").trim();
      const name = configuredName || "Athlete";
      const disciplines = Array.isArray(athlete.disciplines)
        ? athlete.disciplines.map((value) => String(value || "").trim()).filter(Boolean)
        : [];
      const experience = String(athlete.experience_level || "").trim();
      const profileComplete = Boolean(
        athlete.timezone
        && ["metric", "imperial"].includes(athlete.unit_system)
        && disciplines.length
        && (experience || athlete.experience_years != null)
        && athlete.weekly_availability
      );
      const descriptor = [disciplines.join(", "), experience].filter(Boolean).join(" / ");
      const initials = configuredName
        ? configuredName.split(/\\s+/).slice(0, 2).map((part) => part[0]?.toUpperCase() || "").join("")
        : "A";
      const avatar = document.getElementById("rider-avatar");
      const nameNode = document.getElementById("rider-name");
      const description = document.getElementById("rider-description");
      if (avatar) avatar.textContent = initials || "A";
      if (nameNode) nameNode.textContent = name;
      if (description) {
        description.textContent = profileComplete
          ? (descriptor || `${athlete.unit_system} rider`)
          : "Profile setup incomplete";
      }
    }

    function utcDate(value) {
      return new Date(`${value}T00:00:00Z`);
    }

    function monthKey(value) {
      return value.slice(0, 7);
    }

    function monthLabel(key) {
      return MONTH_FORMAT.format(utcDate(`${key}-01`));
    }

    function dayLabel(value) {
      return SHORT_DATE_FORMAT.format(utcDate(value));
    }

    function longDayLabel(value) {
      return LONG_DATE_FORMAT.format(utcDate(value));
    }

    function dayByDate(date) {
      return DAY_BY_DATE.get(date);
    }

    function weekForDate(value) {
      const exactWeek = WEEK_BY_START.get(dayByDate(value)?.week_start);
      return exactWeek || DATA.weeks.find((week) => week.start_date <= value && value <= week.end_date);
    }

    function applyWeekActivityDetails(weekStart) {
      const week = WEEK_BY_START.get(weekStart);
      const payload = ACTIVITY_DETAILS[weekStart];
      if (!week || !payload) return false;
      if (week.activity_details_key && payload.cache_key !== week.activity_details_key) return false;
      const details = payload.days || payload;
      if (!details || typeof details !== "object" || Array.isArray(details)) return false;
      for (const [date, activities] of Object.entries(details)) {
        const day = DAY_BY_DATE.get(date);
        if (day && Array.isArray(activities)) day.activities = activities;
      }
      week.activityDetailsLoaded = true;
      week.activityDetailsLoading = false;
      return true;
    }

    function loadWeekActivityDetails(weekStart) {
      const week = WEEK_BY_START.get(weekStart);
      if (!week?.has_activity_details) return Promise.resolve(false);
      if (week.activityDetailsLoaded) return Promise.resolve(true);
      if (applyWeekActivityDetails(weekStart)) return Promise.resolve(true);
      if (ACTIVITY_DETAIL_LOADS.has(weekStart)) return ACTIVITY_DETAIL_LOADS.get(weekStart);

      const load = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        const version = encodeURIComponent(DATA.generatedAt || "");
        const filename = week.activity_details_file || `${weekStart}.js`;
        const expectedFilename = week.activity_details_key
          ? `${weekStart}.${week.activity_details_key}.js`
          : `${weekStart}.js`;
        if (
          filename !== expectedFilename ||
          (week.activity_details_key && !/^[a-f0-9]{64}$/.test(week.activity_details_key))
        ) {
          reject(new Error(`Ride detail filename did not match week ${weekStart}.`));
          return;
        }
        script.src = `./training_center_activity_details/${encodeURIComponent(filename)}?v=${version}`;
        script.async = true;
        script.onload = () => {
          script.remove();
          if (!applyWeekActivityDetails(weekStart)) {
            reject(new Error(`Ride details did not match week ${weekStart}.`));
            return;
          }
          resolve(true);
        };
        script.onerror = () => {
          script.remove();
          reject(new Error(`Could not load ride details for week ${weekStart}.`));
        };
        document.head.appendChild(script);
      }).finally(() => ACTIVITY_DETAIL_LOADS.delete(weekStart));
      ACTIVITY_DETAIL_LOADS.set(weekStart, load);
      return load;
    }

    function hydrateWeekActivityDetails(weekStart) {
      const week = WEEK_BY_START.get(weekStart);
      if (!week?.has_activity_details || week.activityDetailsLoaded || week.activityDetailsLoading || week.activityDetailsFailed) return;
      week.activityDetailsLoading = true;
      loadWeekActivityDetails(weekStart).then((loaded) => {
        if (!loaded) return;
        if (state.selectedWeekStart === weekStart) renderWeek();
        if (weekForDate(state.selectedDate)?.start_date === weekStart) {
          renderCoachRail();
          renderTodayDashboard();
          renderMonthRail();
          renderRideSidebar();
          if (state.calendarYear === state.selectedDate.slice(0, 4)) renderCalendar();
        }
      }).catch((error) => {
        week.activityDetailsLoading = false;
        week.activityDetailsFailed = true;
        console.warn(error);
      });
    }

    function formatNumber(value, digits = 0) {
      const number = Number(value || 0);
      if (!Number.isFinite(number)) return digits ? "0.0" : "0";
      return number.toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits
      });
    }

    function formatTssNumber(value) {
      const number = Number(value);
      return value == null || !Number.isFinite(number) || number < 0 ? "--"
        : number.toLocaleString(undefined, { maximumFractionDigits: 0 });
    }

    function formatCoverageNumber(value) {
      const number = Number(value);
      return value == null || !Number.isFinite(number) || number < 0 || number > 100 ? "--"
        : number.toLocaleString(undefined, { maximumFractionDigits: 1 });
    }

    function eventIsSkipped(event) {
      return Boolean(event?.markers?.skip) || ["skip", "skipped", "cancelled", "canceled"].includes(String(event?.status || "").toLowerCase());
    }

    function eventText(day, includeSkipped = true) {
      return (day.events || [])
        .filter((event) => includeSkipped || !eventIsSkipped(event))
        .map((event) => `${event.name || ""} ${event.discipline || ""} ${event.raw || ""}`)
        .join(" ");
    }

    function dayIntentText(day, includeSkippedEvents = true) {
      return `${day.planned || ""} ${day.actual || ""} ${day.source_note || ""} ${eventText(day, includeSkippedEvents)}`;
    }

    function hasRaceSignal(day) {
      return (day.events || []).some((event) => !eventIsSkipped(event));
    }

    function raceKind(day) {
      if (!hasRaceSignal(day)) return "";
      const text = eventText(day, false).toLowerCase();
      if (/\\b(gravel|dirt|mtb|mountain|singletrack|hopper|cyclocross|cx)\\b/.test(text)) return "dirt";
      if (/\\b(road|crit|criterium|tt|time trial|circuit|grand prix|giro)\\b/.test(text)) return "road";
      return "road";
    }

    function hasIntervalSignal(day) {
      return Boolean(day.planned_hard_day || day.hard_activity) ||
        /\\b(interval|vo2|threshold|sweet spot|over.?under|tabata|anaerobic|sprint|tempo|2x|3x|4x|5x|6x|ss\\b|zone 4|z4|z5)\\b/i.test(dayIntentText(day));
    }

    function plannedMaxHours(day) {
      if (Number(day?.planned_load?.hours_max || 0) > 0) return Number(day.planned_load.hours_max);
      const text = String(day.planned || "").replace(/[–—]/g, "-");
      let maxHours = 0;
      for (const match of text.matchAll(/\\b(\\d+(?:\\.\\d+)?)\\s*(?:-|to)\\s*(\\d+(?:\\.\\d+)?)\\s*(?:h|hr|hrs|hour|hours)\\b/gi)) {
        maxHours = Math.max(maxHours, Number(match[1]), Number(match[2]));
      }
      for (const match of text.matchAll(/\\b(\\d+(?:\\.\\d+)?)\\s*\\+\\s*(?:h|hr|hrs|hour|hours)\\b/gi)) {
        maxHours = Math.max(maxHours, Number(match[1]));
      }
      for (const match of text.matchAll(/\\b(\\d+(?:\\.\\d+)?)\\s*(?:h|hr|hrs|hour|hours)\\b/gi)) {
        maxHours = Math.max(maxHours, Number(match[1]));
      }
      return maxHours;
    }

    function hasBigSignal(day) {
      const metrics = day.metrics || {};
      return Number(metrics.meaningful_ride_hours || 0) >= BIG_DAY_HOURS ||
        Number(metrics.estimated_tss || 0) >= BIG_DAY_TSS ||
        plannedMaxHours(day) >= BIG_DAY_HOURS;
    }

    function calendarDaySignals(day) {
      const race = hasRaceSignal(day);
      return {
        interval: hasIntervalSignal(day),
        race,
        raceKind: race ? raceKind(day) : "",
        big: hasBigSignal(day),
        ride: Boolean(day.metrics?.activity_count),
        event: Boolean(day.events?.length),
        note: hasDailyNote(day),
        coachNote: hasCoachNote(day)
      };
    }

    function authoredBadgeItems(day) {
      return (day.dashboard_labels || [])
        .filter((item) => item && item.label)
        .map((item) => ({
          kind: "custom",
          label: item.label,
          shortLabel: item.short || item.label.slice(0, 1),
          title: item.title || item.label
        }));
    }

    function dayReaction(day) {
      const primary = primaryActivity(day);
      if (primary?.reaction) return primary.reaction;
      return (day.activities || []).find((activity) => activity?.reaction)?.reaction || "";
    }

    function dayBadgeItems(day, signals) {
      const tags = [];
      if (signals.race) tags.push({ kind: signals.raceKind === "dirt" ? "race-dirt" : "race-road", label: "Race", shortLabel: "R", title: "Race" });
      if (signals.interval) tags.push({ kind: "interval", label: "Interval", shortLabel: "I", title: "Interval" });
      if (signals.big) tags.push({ kind: "big", label: "Big", shortLabel: "B", title: BIG_DAY_LABEL });
      if (signals.event && !signals.race) tags.push({ kind: "event", label: "Event", shortLabel: "E", title: "Event" });
      if (signals.coachNote) tags.push({ kind: "coach-note", label: "Coach", shortLabel: "C", title: "Coach" });
      if (signals.note) tags.push({ kind: "note", label: "Note", shortLabel: "N", title: "Note" });
      tags.push(...authoredBadgeItems(day));
      const reaction = dayReaction(day);
      if (reaction) tags.push({ kind: "reaction", label: reaction, shortLabel: reaction, title: "Coach reaction" });
      return tags;
    }

    function renderDayTags(day, signals) {
      const tags = dayBadgeItems(day, signals);
      return tags.length
        ? `<span class="day-tags">${tags.map(({ kind, label, title }) => `<span class="day-tag ${kind}"${title ? ` title="${escapeHtml(title)}"` : ""}>${escapeHtml(label)}</span>`).join("")}</span>`
        : "";
    }

    function weekSignalItems(day, signals) {
      return dayBadgeItems(day, signals);
    }

    function renderWeekDaySignals(day, signals) {
      const items = weekSignalItems(day, signals);
      if (!items.length) return "";
      return `
        <button class="week-day-signals" type="button" aria-expanded="false" aria-label="Show day signals">
          ${items.map(({ kind, shortLabel, label, title }) => `<span class="day-tag week-day-signal ${kind}" data-short="${escapeHtml(shortLabel)}" data-full="${escapeHtml(label)}"${title ? ` title="${escapeHtml(title)}"` : ""}>${escapeHtml(shortLabel)}</span>`).join("")}
        </button>`;
    }

    function renderDayKpi(day) {
      const metrics = day.metrics || {};
      const parts = [];
      if (Number(metrics.meaningful_ride_hours || 0) > 0) parts.push(`${formatNumber(metrics.meaningful_ride_hours, 1)}h`);
      if (metrics.estimated_tss != null) parts.push(dayTssLabel(day));
      if (Number(metrics.kilojoules || 0) > 0) parts.push(`${formatNumber(metrics.kilojoules, 0)} kJ`);
      return parts.length ? `<span class="day-kpi" title="${escapeHtml(metrics.tss_description || "")}">${escapeHtml(parts.slice(0, 2).join(" / "))}</span>` : "";
    }

    function monthStats(days) {
      const stats = {
        meaningfulHours: 0,
        estimatedTss: 0,
        tssDays: 0,
        tssEstimated: false,
        tssPartial: false,
        tssPowerPartial: false,
        tssMissingRides: 0,
        tssPowerLoadSeconds: 0,
        tssPowerReportedSeconds: 0,
        kilojoules: 0,
        raceDays: 0,
        intervalDays: 0,
        bigDays: 0,
        rideDays: 0,
        noteDays: 0,
        coachNoteDays: 0
      };
      for (const day of days) {
        const metrics = day.metrics || {};
        const signals = calendarDaySignals(day);
        stats.meaningfulHours += Number(metrics.meaningful_ride_hours || 0);
        stats.estimatedTss += Number(metrics.estimated_tss || 0);
        if (metrics.estimated_tss != null) stats.tssDays += 1;
        stats.tssMissingRides += Number(metrics.tss_missing_activity_count || 0);
        stats.tssPowerLoadSeconds += Number(metrics.tss_power_load_duration_s || 0);
        stats.tssPowerReportedSeconds += Number(metrics.tss_power_reported_duration_s || 0);
        stats.tssEstimated ||= Boolean(metrics.tss_estimated);
        stats.tssPartial ||= Boolean(metrics.tss_partial);
        stats.tssPowerPartial ||= Boolean(metrics.tss_power_incomplete);
        stats.kilojoules += Number(metrics.kilojoules || 0);
        if (signals.race) stats.raceDays += 1;
        if (signals.interval) stats.intervalDays += 1;
        if (signals.big) stats.bigDays += 1;
        if (signals.ride) stats.rideDays += 1;
        if (signals.note) stats.noteDays += 1;
        if (signals.coachNote) stats.coachNoteDays += 1;
      }
      return stats;
    }

    function coachNotesForDay(day) {
      if (!day) return [];
      const notes = DATA.coachNotes?.[day.date];
      return Array.isArray(notes) ? notes : [];
    }

    function hasCoachNote(day) {
      return coachNotesForDay(day).length > 0;
    }

    function noteForDay(day) {
      if (!day) return "";
      const entry = state.notes[day.date];
      if (entry && Object.prototype.hasOwnProperty.call(entry, "note")) return entry.note || "";
      return day.source_note || "";
    }

    function hasDailyNote(day) {
      return Boolean(noteForDay(day).trim());
    }

    function renderDailyNoteEditor(day, options = {}) {
      const id = options.id || `daily-note-${day.date}`;
      const className = options.className || "rail-note-label";
      const placeholder = options.placeholder || "Add note...";
      return `
        <label class="${escapeHtml(className)}" for="${escapeHtml(id)}">
          <span>Daily note</span>
          <textarea id="${escapeHtml(id)}" data-inline-date="${escapeHtml(day.date)}" maxlength="500" placeholder="${escapeHtml(placeholder)}">${escapeHtml(noteForDay(day))}</textarea>
        </label>`;
    }

    function syncNoteInputs(date, value, source = null) {
      document.querySelectorAll(`[data-inline-date="${date}"]`).forEach((node) => {
        if (node !== source && node.value !== value) node.value = value;
      });
    }

    function setStatus(message) {
      const status = document.getElementById("status-text");
      if (status) status.textContent = message;
    }

    function storeFlashStatus(message) {
      try {
        window.sessionStorage.setItem(FLASH_STATUS_STORAGE_KEY, message);
      } catch (error) {
        return;
      }
    }

    function showFlashStatus() {
      try {
        const message = window.sessionStorage.getItem(FLASH_STATUS_STORAGE_KEY);
        if (!message) return;
        window.sessionStorage.removeItem(FLASH_STATUS_STORAGE_KEY);
        setStatus(message);
      } catch (error) {
        return;
      }
    }

    function updateWriteToken(payload) {
      if (payload?.write_token) {
        state.writeToken = payload.write_token;
      }
    }

    function apiHeaders(headers = {}) {
      const merged = { "accept": "application/json", ...headers };
      if (state.writeToken) {
        merged["x-coach-write-token"] = state.writeToken;
      }
      return merged;
    }

    async function loadNotes() {
      const seed = DATA.notes || {};
      state.notes = { ...seed };
      try {
        const response = await fetch(NOTES_API, {
          headers: { "accept": "application/json" },
          cache: "no-store"
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        updateWriteToken(payload);
        const apiNotes = payload.notes && !Array.isArray(payload.notes) ? payload.notes : {};
        state.notes = { ...apiNotes };
        state.notesWritable = Boolean(payload.writable);
        state.notesPath = payload.path || "configured daily_notes.json";
      } catch (error) {
        state.notesWritable = false;
      }
    }

    function updateNote(date, note, options = {}) {
      const cleanNote = String(note || "");
      const day = dayByDate(date);
      const shouldPreserveEmpty = Boolean((day?.source_note || "").trim());
      if (cleanNote.trim() || shouldPreserveEmpty) {
        state.notes[date] = {
          date,
          note: cleanNote,
          updated_at: new Date().toISOString(),
          source: "training_center"
        };
      } else {
        delete state.notes[date];
      }
      updateNoteMarkers(date);
      if (options.skipSave) return;
      if (state.notesWritable) {
        queueNoteSave(date, options.immediate);
      } else {
        setStatus("Not saved yet: start with python -m gradient_ascent.cli serve-training-center to persist notes.");
      }
    }

    function queueNoteSave(date, immediate = false) {
      const existing = state.noteSaveTimers.get(date);
      if (existing) window.clearTimeout(existing);
      const save = () => {
        state.noteSaveTimers.delete(date);
        saveNote(date);
      };
      if (immediate) {
        save();
        return;
      }
      state.noteSaveTimers.set(date, window.setTimeout(save, 350));
      setStatus(`Saving to ${state.notesPath}...`);
    }

    async function saveNote(date) {
      const entry = state.notes[date];
      const payload = {
        note: entry?.note || "",
        updated_at: entry?.updated_at || new Date().toISOString(),
        source: entry?.source || "training_center",
        preserve_empty: Boolean((dayByDate(date)?.source_note || "").trim())
      };
      try {
        const response = await fetch(`${NOTES_API}/${encodeURIComponent(date)}`, {
          method: "PUT",
          headers: apiHeaders({ "content-type": "application/json" }),
          body: JSON.stringify(payload)
        });
        if (!response.ok) {
          const errorPayload = await response.json().catch(() => ({}));
          throw new Error(errorPayload.error || `HTTP ${response.status}`);
        }
        const result = await response.json();
        if (result.entry) {
          state.notes[date] = result.entry;
        } else {
          delete state.notes[date];
        }
        if (result.path) {
          state.notesPath = result.path;
        }
        updateNoteMarkers(date);
        setStatus(`Saved to ${state.notesPath}`);
      } catch (error) {
        setStatus(`Save failed: ${error.message || error}. Notes remain in memory; export if needed.`);
      }
    }

    function updateSyncButton(payload = {}) {
      const button = document.getElementById("sync-button");
      if (!button) return;
      const running = Boolean(payload.running || payload.status === "running");
      button.disabled = running;
      button.classList.toggle("sync-running", running);
      button.setAttribute("aria-label", running ? "Refreshing data" : "Refresh data");
      button.title = running ? "Refreshing data" : "Refresh data";
      if (payload.message && (running || payload.status === "failed")) {
        setStatus(payload.message);
      }
    }

    function syncProviderSummary(payload = {}) {
      for (const step of payload.steps || []) {
        try {
          const summary = JSON.parse(step.output || "{}").provider_sync?.ridewithgps;
          if (summary?.status === "synced") {
            const continuation = summary.has_more ? "; more history is available" : "";
            return `Ride with GPS: ${summary.imported || 0} new, ${summary.updated || 0} updated, ${summary.existing || 0} unchanged${continuation}`;
          }
        } catch (_) { /* Older local companions may emit a different summary. */ }
      }
      const providerSteps = (payload.steps || []).filter((step) =>
        String(step.name || "").endsWith(" sync") && step.command?.length === 0
      );
      if (!providerSteps.length) return "";
      return providerSteps
        .map((step) => `${String(step.name).replace(/ sync$/, "")} ${step.ok ? "ok" : "failed"}`)
        .join(", ");
    }

    async function fetchSyncStatus() {
      const response = await fetch(SYNC_API, {
        headers: { "accept": "application/json" },
        cache: "no-store"
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      updateWriteToken(payload);
      return payload;
    }

    function queueSyncPoll() {
      if (state.syncPollTimer) {
        window.clearTimeout(state.syncPollTimer);
      }
      state.syncPollTimer = window.setTimeout(pollSyncStatus, 2500);
    }

    async function pollSyncStatus() {
      try {
        const payload = await fetchSyncStatus();
        updateSyncButton(payload);
        if (payload.running || payload.status === "running") {
          queueSyncPoll();
          return;
        }
        if (payload.status === "completed") {
          const providerSummary = syncProviderSummary(payload);
          const suffix = providerSummary ? ` (${providerSummary})` : "";
          storeFlashStatus(`${payload.message || "Sync complete"}${suffix}`);
          setStatus(`${payload.message || "Sync complete"}${suffix}; reloading...`);
          window.setTimeout(() => {
            const nextUrl = new URL(window.location.href);
            nextUrl.searchParams.set("reload", Date.now().toString());
            window.location.replace(nextUrl.toString());
          }, 900);
        } else if (payload.status === "failed") {
          setStatus(payload.message || "Sync failed. Check the server logs.");
        }
      } catch (error) {
        updateSyncButton({ status: "failed" });
        setStatus(`Sync status unavailable: ${error.message || error}`);
      }
    }

    async function startSync(options = {}) {
      const button = document.getElementById("sync-button");
      if (button?.disabled) return;
      updateSyncButton({ running: true, message: "Starting sync..." });
      try {
        const response = await fetch(SYNC_API, {
          method: "POST",
          headers: apiHeaders({ "content-type": "application/json" }),
          body: JSON.stringify(options)
        });
        if (!response.ok) {
          const errorPayload = await response.json().catch(() => ({}));
          throw new Error(errorPayload.error || `HTTP ${response.status}`);
        }
        const payload = await response.json();
        updateSyncButton(payload);
        queueSyncPoll();
      } catch (error) {
        updateSyncButton({ status: "failed" });
        setStatus(`Sync needs the local server: ${error.message || error}`);
      }
    }

    async function loadSyncStatus() {
      try {
        const payload = await fetchSyncStatus();
        updateSyncButton(payload);
        if (payload.running || payload.status === "running") {
          queueSyncPoll();
        }
      } catch (error) {
        updateSyncButton({ status: "idle" });
      }
    }

    async function loadConnections() {
      try {
        const response = await fetch(CONNECTIONS_API, {
          headers: { "accept": "application/json" },
          cache: "no-store"
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        updateWriteToken(payload);
        state.connections = payload;
      } catch (error) {
        state.connections = null;
      }
    }

    function updateNoteMarkers(date) {
      document.querySelectorAll(`[data-date="${date}"]`).forEach((node) => {
        node.classList.toggle("has-note", hasDailyNote(dayByDate(date)));
      });
    }

    function renderSourceMeta() {
      const canonical = DATA.postSyncSummary?.canonical || {};
      const activities = canonical.activities ?? "--";
      const recovery = canonical.recovery ?? "--";
      document.getElementById("calendar-meta").textContent = `${DATA.weeks.length} weeks / ${activities} activities / ${recovery} recovery records`;
    }

    function syncScrollState() {
      const rail = document.querySelector(".context-stack");
      const hasMoved = window.scrollY > 8 || Number(rail?.scrollTop || 0) > 8;
      document.body.classList.toggle("has-scrolled", hasMoved);
    }

    function syncNavigationUrl() {
      const url = new URL(window.location.href);
      url.searchParams.set("view", state.view);
      if (/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(state.selectedDate || "")) url.searchParams.set("date", state.selectedDate);
      if (state.view === "calendar" && /^[0-9]{4}$/.test(state.calendarYear || "")) url.searchParams.set("year", state.calendarYear);
      else url.searchParams.delete("year");
      if (url.href !== window.location.href) window.history.replaceState(window.history.state, "", url.href);
    }

    function setView(view) {
      state.view = view;
      document.body.dataset.view = view;
      document.body.classList.toggle("primary-shell", PRIMARY_VIEWS.has(view));
      if (view === "calendar") renderSeasonOverview();
      if (view === "connections") {
        renderConnections();
        if (state.writeToken) void loadRideSetupStatus();
      }
      document.querySelectorAll(".tab[data-view]").forEach((button) => {
        button.classList.toggle("active", button.dataset.view === view);
      });
      document.querySelectorAll(".view").forEach((viewNode) => {
        viewNode.classList.toggle("active", viewNode.id === `${view}-view`);
      });
      if (view === "today") {
        requestAnimationFrame(centerSelectedMonthDay);
      }
      syncNavigationUrl();
    }

    function connectionStatusLabel(provider) {
      return String(provider.status || "needs_setup").replace(/_/g, " ");
    }

    function connectionTierLabel(provider) {
      return String(provider.support_tier || "").replace(/_/g, " ");
    }

    function connectionFieldInput(provider, field) {
      const configured = Boolean(provider.configured_fields?.[field.key]);
      return `
        <label class="connection-field">
          <span>${escapeHtml(field.label)}</span>
          <input
            name="${escapeHtml(field.key)}"
            type="text"
            placeholder="${configured ? "saved" : ""}"
            autocomplete="off"
          />
        </label>`;
    }

    function renderStravaArchiveSetup(provider) {
      if (!provider.archive_upload_available) return "";
      const exportUrl = provider.export_url || STRAVA_EXPORT_URL;
      return `
        <section class="connection-archive">
          <p class="connection-copy">Request Strava's official account archive, then upload the downloaded ZIP here to seed local history.</p>
          <div class="connection-actions">
            <a href="${escapeHtml(exportUrl)}" target="_blank" rel="noreferrer">Request Strava export</a>
          </div>
          <label class="connection-upload">
            <span>Downloaded Strava archive</span>
            <input type="file" accept=".zip,application/zip,text/csv,.csv" data-role="strava-archive" />
          </label>
          <div class="connection-actions">
            <button type="button" class="primary" data-action="upload-strava-archive">Import archive</button>
          </div>
        </section>`;
    }

    function rideAuthorizationUrl(value) {
      try {
        const url = new URL(String(value || ""));
        return url.origin === "https://ridewithgps.com" && url.pathname === "/oauth/authorize"
          ? url.toString() : "";
      } catch (_) { return ""; }
    }

    function renderRideWithGPSSetup(provider) {
      if (provider.key !== "ridewithgps") return "";
      const ride = provider.ride || {};
      const job = state.rideSetup || {};
      const running = Boolean(job.running);
      const authorizationUrl = rideAuthorizationUrl(job.authorization_url);
      const history = ride.last_sync?.mode === "history" ? ride.last_sync : null;
      const historyLabel = history?.has_more ? "Continue importing older rides" : "Import older rides";
      const summary = ride.last_sync;
      return `
        <section class="connection-archive" data-role="ride-setup">
          <p class="connection-copy">Use the official, checksum-verified <code>ride</code> app. Sign in on Ride with GPS in the browser profile you choose; no API key or password is entered here.</p>
          ${!ride.installed ? '<p class="connection-note">Install and connect downloads the official CLI into this private workspace (about 60–105 MB).</p>' : ""}
          ${summary ? `<p class="connection-health">Last batch: ${Number(summary.imported || 0)} new, ${Number(summary.updated || 0)} updated, ${Number(summary.existing || 0)} unchanged.${summary.has_more ? " More history is available." : ""}</p>` : ""}
          ${job.message && job.status !== "idle" ? `<p class="connection-setup-status" role="status">${escapeHtml(job.message)}</p>` : ""}
          <div class="connection-actions">
            ${authorizationUrl && running ? `<a class="primary" data-role="ride-authorization" href="${escapeHtml(authorizationUrl)}" target="_blank" rel="noreferrer noopener">Open Ride with GPS sign-in</a><button type="button" data-action="ride-cancel">Cancel sign-in</button>` : ""}
            ${running && !authorizationUrl ? '<button type="button" disabled>Working…</button>' : ""}
            ${!running && !ride.enabled ? `<button type="button" class="primary" data-action="${ride.installed ? "ride-connect" : "ride-install"}">${ride.installed ? "Connect Ride with GPS" : "Install and connect"}</button>` : ""}
            ${!running && ride.enabled ? '<button type="button" class="primary" data-action="ride-sync">Sync recent rides</button>' : ""}
            ${!running && ride.installed ? '<button type="button" data-action="ride-reauth">Reconnect / choose account</button><button type="button" data-action="ride-check">Check</button>' : ""}
            ${!running && ride.enabled ? `<button type="button" data-action="ride-history">${historyLabel}</button><button type="button" data-action="ride-disable">Stop syncing</button>` : ""}
          </div>
          <p class="connection-note">Older history imports in resumable batches. Stopping sync keeps your imported rides and leaves the vendor's sign-in unchanged.</p>
        </section>`;
    }

    function renderConnectionCard(provider) {
      const fields = Array.isArray(provider.fields) ? provider.fields : [];
      const issues = Array.isArray(provider.issues) ? provider.issues : [];
      const steps = Array.isArray(provider.next_steps) ? provider.next_steps : [];
      const notes = Array.isArray(provider.notes) ? provider.notes : [];
      const noteItems = [...new Set([...issues, ...notes])];
      const statusLabel = connectionStatusLabel(provider);
      const tierLabel = connectionTierLabel(provider);
      const healthItems = [
        provider.last_import_at ? `Last import ${provider.last_import_at}` : null,
      ].filter(Boolean);
      return `
        <article class="connection-card" data-provider="${escapeHtml(provider.key)}">
          <div class="connection-card-head">
            <div>
              <h3>${escapeHtml(provider.label)}</h3>
              <p class="connection-copy">${escapeHtml(provider.summary || "")}</p>
            </div>
            <div class="connection-badges">
              <span class="connection-status ${escapeHtml(provider.status || "")}">${escapeHtml(statusLabel)}</span>
              ${tierLabel !== statusLabel ? `<span class="connection-tier ${escapeHtml(provider.support_tier || "")}">${escapeHtml(tierLabel)}</span>` : ""}
            </div>
          </div>
          ${noteItems.map((item) => `<p class="connection-note">${escapeHtml(item)}</p>`).join("")}
          ${healthItems.map((item) => `<p class="connection-health">${escapeHtml(item)}</p>`).join("")}
          ${renderStravaArchiveSetup(provider)}
          ${renderRideWithGPSSetup(provider)}
          ${fields.length ? `
            <form class="connection-form">
              ${fields.map((field) => connectionFieldInput(provider, field)).join("")}
            </form>` : ""}
          ${steps.length ? `<p class="connection-steps">${escapeHtml(steps.join(" "))}</p>` : ""}
          <div class="connection-actions">
            ${fields.length ? '<button type="button" class="primary" data-action="save">Save</button>' : ""}
            ${provider.test_available && provider.key !== "ridewithgps" ? '<button type="button" data-action="test">Check</button>' : ""}
          </div>
        </article>`;
    }

    function renderConnections() {
      const root = document.getElementById("connections-root");
      if (!root) return;
      const providers = [...(state.connections?.providers || [])].sort((a, b) => Number(b.key === "ridewithgps") - Number(a.key === "ridewithgps"));
      if (!providers.length) {
        root.innerHTML = '<div class="connection-empty">No provider data available.</div>';
        return;
      }
      const available = `
        <section class="connection-section">
          <div class="connection-section-head">
            <h3>Available now</h3>
            <span>${providers.length} source${providers.length === 1 ? "" : "s"}</span>
          </div>
          <div class="connection-grid">${providers.map(renderConnectionCard).join("")}</div>
        </section>`;
      root.innerHTML = available;
      bindConnectionCards();
    }

    async function refreshConnections() {
      await loadConnections();
      renderConnections();
    }

    function queueRideSetupPoll() {
      if (state.rideSetupPollTimer) window.clearTimeout(state.rideSetupPollTimer);
      state.rideSetupPollTimer = window.setTimeout(loadRideSetupStatus, 750);
    }

    async function loadRideSetupStatus() {
      if (!state.writeToken) return;
      try {
        const response = await fetch(RIDE_SETUP_API, {
          headers: apiHeaders({ "accept": "application/json" }),
          cache: "no-store"
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        updateWriteToken(payload);
        const wasRunning = Boolean(state.rideSetup?.running);
        state.rideSetup = payload;
        if (payload.running) queueRideSetupPoll();
        else if (wasRunning) {
          await loadConnections();
          setStatus(payload.message || "Ride with GPS setup finished.");
        }
        if (state.view === "connections") renderConnections();
      } catch (error) {
        state.rideSetup = { status: "failed", running: false, message: error.message || "Setup status unavailable." };
        if (state.view === "connections") renderConnections();
      }
    }

    async function rideSetupAction(action, options = {}) {
      if (!state.writeToken) await loadConnections();
      const response = await fetch(RIDE_SETUP_API, {
        method: "POST",
        headers: apiHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ action, ...options })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      updateWriteToken(payload);
      state.rideSetup = payload;
      renderConnections();
      if (payload.running) queueRideSetupPoll();
      else await refreshConnections();
    }

    async function saveConnection(providerKey, card) {
      const fields = {};
      card.querySelectorAll("input[name]").forEach((input) => {
        if (input.value.trim()) fields[input.name] = input.value.trim();
      });
      const response = await fetch(`${CONNECTIONS_API}/${providerKey}`, {
        method: "PUT",
        headers: apiHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ fields })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      await refreshConnections();
      setStatus(`Saved ${providerKey} connection settings.`);
    }

    async function testConnection(providerKey) {
      const response = await fetch(`${CONNECTIONS_API}/${providerKey}/test`, {
        method: "POST",
        headers: apiHeaders()
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      const suffix = payload.issues?.length ? ` ${payload.issues.join(" ")}` : "";
      setStatus(`${providerKey}: ${payload.status}.${suffix}`);
      await refreshConnections();
    }

    async function uploadStravaArchive(card) {
      const input = card.querySelector('[data-role="strava-archive"]');
      const file = input?.files?.[0];
      if (!file) throw new Error("Choose a downloaded Strava archive ZIP first.");
      const button = card.querySelector('[data-action="upload-strava-archive"]');
      if (button) button.disabled = true;
      setStatus(`Uploading ${file.name}...`);
      try {
        const response = await fetch(STRAVA_ARCHIVE_API, {
          method: "POST",
          headers: apiHeaders({
            "content-type": file.type || (file.name.toLowerCase().endsWith(".csv") ? "text/csv" : "application/zip"),
            "x-coach-upload-name": encodeURIComponent(file.name)
          }),
          body: file
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        const rows = payload.import?.rows ?? 0;
        const recordings = payload.import?.recordings_parsed ?? 0;
        const streams = payload.import?.streams_created ?? 0;
        const laps = payload.import?.laps_created ?? 0;
        const failures = (payload.import?.recordings_missing ?? 0)
          + (payload.import?.recordings_unsupported ?? 0)
          + (payload.import?.recordings_failed ?? 0);
        const suffix = recordings
          ? ` Parsed ${recordings} recordings into ${streams} stream files and ${laps} lap files.`
          : "";
        const warning = failures ? ` ${failures} recordings need attention.` : "";
        const message = `Imported ${rows} Strava archive rows.${suffix}${warning}`;
        storeFlashStatus(message);
        setStatus(`${message} Reloading...`);
        window.setTimeout(() => {
          const nextUrl = new URL(window.location.href);
          nextUrl.searchParams.set("view", "connections");
          nextUrl.searchParams.set("reload", Date.now().toString());
          window.location.replace(nextUrl.toString());
        }, 500);
      } finally {
        if (button) button.disabled = false;
      }
    }

    function isActivityRecording(file) {
      return /\\.(fit|tcx|gpx)$/i.test(String(file?.name || ""));
    }

    function activityRecordingContentType(file) {
      const name = String(file?.name || "").toLowerCase();
      if (name.endsWith(".tcx")) return "application/vnd.garmin.tcx+xml";
      if (name.endsWith(".gpx")) return "application/gpx+xml";
      return "application/octet-stream";
    }

    async function uploadActivityRecording(file, index, total) {
      setStatus(`Importing ${file.name} (${index} of ${total})...`);
      const response = await fetch(ACTIVITY_RECORDINGS_API, {
        method: "POST",
        headers: apiHeaders({
          "content-type": file.type || activityRecordingContentType(file),
          "x-coach-upload-name": encodeURIComponent(file.name)
        }),
        body: file
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    }

    async function uploadActivityRecordings(fileList) {
      const candidates = Array.from(fileList || []);
      const rejected = candidates.filter((file) => !isActivityRecording(file));
      if (rejected.length) {
        throw new Error(`Unsupported ride file: ${rejected[0].name}. Use FIT, TCX, or GPX.`);
      }
      const files = candidates.filter(isActivityRecording);
      if (!files.length) throw new Error("Drop a FIT, TCX, or GPX activity recording.");
      let created = 0;
      for (const [index, file] of files.entries()) {
        const payload = await uploadActivityRecording(file, index + 1, files.length);
        if (payload.import?.created) created += 1;
      }
      const duplicateCount = files.length - created;
      const duplicateNote = duplicateCount
        ? ` ${duplicateCount} duplicate ${duplicateCount === 1 ? "was" : "were"} refreshed.`
        : "";
      const message = `Imported ${created} new ride ${created === 1 ? "file" : "files"}.${duplicateNote}`;
      storeFlashStatus(message);
      setStatus(`${message} Reloading...`);
      window.setTimeout(() => {
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set("reload", Date.now().toString());
        window.location.replace(nextUrl.toString());
      }, 500);
    }

    function openActivityRecordingPicker() {
      document.getElementById("activity-recording-input")?.click();
    }

    function bindActivityRecordingImport() {
      const overlay = document.getElementById("recording-drop-overlay");
      const input = document.getElementById("activity-recording-input");
      let dragDepth = 0;
      const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
      const hideOverlay = () => {
        dragDepth = 0;
        overlay.hidden = true;
        overlay.setAttribute("aria-hidden", "true");
      };
      window.addEventListener("dragenter", (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        dragDepth += 1;
        overlay.hidden = false;
        overlay.setAttribute("aria-hidden", "false");
      });
      window.addEventListener("dragover", (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      });
      window.addEventListener("dragleave", (event) => {
        if (overlay.hidden) return;
        dragDepth = Math.max(0, dragDepth - 1);
        if (!dragDepth) hideOverlay();
      });
      window.addEventListener("dragend", hideOverlay);
      window.addEventListener("drop", (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        const files = event.dataTransfer.files;
        hideOverlay();
        uploadActivityRecordings(files).catch((error) => setStatus(error.message || error));
      });
      input?.addEventListener("change", () => {
        const files = input.files;
        uploadActivityRecordings(files)
          .catch((error) => setStatus(error.message || error))
          .finally(() => { input.value = ""; });
      });
      document.getElementById("import-ride-file")?.addEventListener("click", () => {
        closeActionMenu();
        openActivityRecordingPicker();
      });
      document.getElementById("connections-import-ride-file")?.addEventListener(
        "click",
        openActivityRecordingPicker,
      );
    }

    function bindConnectionCards() {
      document.querySelectorAll(".connection-card").forEach((card) => {
        const providerKey = card.dataset.provider;
        card.querySelector('[data-action="save"]')?.addEventListener("click", () => {
          saveConnection(providerKey, card).catch((error) => setStatus(error.message || error));
        });
        card.querySelector('[data-action="test"]')?.addEventListener("click", () => {
          testConnection(providerKey).catch((error) => setStatus(error.message || error));
        });
        card.querySelector('[data-action="upload-strava-archive"]')?.addEventListener("click", () => {
          uploadStravaArchive(card).catch((error) => setStatus(error.message || error));
        });
        const rideActions = {
          "ride-install": () => rideSetupAction("connect", { install: true }),
          "ride-connect": () => rideSetupAction("connect"),
          "ride-reauth": () => rideSetupAction("connect", { reauth: true }),
          "ride-check": () => rideSetupAction("check"),
          "ride-disable": () => rideSetupAction("disable"),
          "ride-cancel": () => rideSetupAction("cancel"),
          "ride-sync": () => startSync(),
          "ride-history": () => startSync({ ride_history: true }),
        };
        for (const [action, callback] of Object.entries(rideActions)) {
          card.querySelector(`[data-action="${action}"]`)?.addEventListener("click", () => {
            Promise.resolve(callback()).catch((error) => setStatus(error.message || error));
          });
        }
      });
    }

    function codexThreadUrl(prompt) {
      const query = new URLSearchParams({ prompt });
      if (DATA.workspacePath) query.set("path", DATA.workspacePath);
      return `codex://new?${query.toString()}`;
    }

    function renderTodayTabLabel() {
      const todayTab = document.querySelector(".today-tab");
      const todayLabel = todayAnchorLabel();
      if (todayTab) {
        todayTab.querySelector("span").textContent = todayLabel;
        todayTab.title = `Open ${todayLabel.toLowerCase()} day`;
      }
    }

    function renderTabs() {
      renderTodayTabLabel();
      document.querySelectorAll(".tab[data-view]").forEach((button) => {
        button.addEventListener("click", () => {
          if (button.dataset.view === "today") {
            refreshCurrentDate();
            const target = todayAnchorDate();
            if (target !== state.selectedDate) {
              selectDate(target, { switchWeek: true, openRide: true });
            }
          }
          setView(button.dataset.view);
        });
      });
    }

    function seasonFocusKey(root) {
      const active = document.activeElement;
      if (!active || !root?.contains(active)) return null;
      const key = active.getAttribute("data-season-focus");
      return typeof key === "string" && /^[a-z0-9-]+$/.test(key) ? key : null;
    }

    function restoreSeasonFocus(root, key) {
      if (!root || !key || !/^[a-z0-9-]+$/.test(key)) return;
      const target = root.querySelector(`[data-season-focus="${key}"]`);
      if (target && !target.disabled) target.focus({ preventScroll: true });
    }

    function renderCalendar() {
      renderSeasonOverview();
      const grouped = new Map();
      const visibleDays = DATA.days.filter((day) => day.date.slice(0, 4) === state.calendarYear);
      for (const day of visibleDays) {
        const key = monthKey(day.date);
        if (!grouped.has(key)) grouped.set(key, []);
        grouped.get(key).push(day);
      }
      const months = [...grouped.entries()].map(([key, days]) => renderMonth(key, days)).join("");
      document.getElementById("calendar-grid").innerHTML = months;
      document.querySelectorAll(".calendar-day").forEach((button) => {
        button.addEventListener("click", () => selectDate(button.dataset.date, { switchWeek: true, openRide: true }));
      });
      document.querySelectorAll("button.calendar-week-stat").forEach((button) => {
        button.addEventListener("click", () => {
          state.selectedWeekStart = button.dataset.weekStart;
          renderWeek();
          setView("weeks");
        });
      });
    }

    function renderSeasonOverview() {
      const root = document.getElementById("season-overview");
      if (!root) return;
      const focus = seasonFocusKey(root);
      const eventsOpen = root.querySelector(".season-event-list")?.open === true;
      const week = DATA.weeks.find((item) => item.start_date === state.selectedWeekStart) || null;
      root.innerHTML = renderSeasonHorizon(week, { scope: "calendar", year: state.calendarYear });
      const events = root.querySelector(".season-event-list");
      if (events && eventsOpen) events.open = true;
      bindSeasonHorizon(root.querySelector("[data-season-jump]"));
      restoreSeasonFocus(root, focus);
    }

    function renderCalendarYearSelect() {
      const select = document.getElementById("calendar-year-select");
      if (!select) return;
      const dates = [
        ...(DATA.days || []).map((day) => day.date),
        ...(DATA.weeks || []).flatMap((week) => [week.start_date, week.end_date]),
        ...(DATA.phases || []).flatMap((phase) => [phase.start_date, phase.end_date]),
        ...(DATA.events || []).map((event) => event.date)
      ];
      const years = [...new Set(dates.filter((value) => typeof value === "string" && /^[0-9]{4}-/.test(value)).map((value) => value.slice(0, 4)))].sort().reverse();
      if (!years.length) years.push(TODAY.slice(0, 4));
      if (!years.includes(state.calendarYear)) state.calendarYear = years[0] || TODAY.slice(0, 4);
      select.innerHTML = years
        .map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(year)}</option>`)
        .join("");
      select.value = state.calendarYear;
    }

    function renderMonth(key, days) {
      const first = utcDate(`${key}-01`);
      const blanks = (first.getUTCDay() + 6) % 7;
      const rows = buildMonthRows(days, blanks);
      const stats = monthStats(days);
      return `
        <article class="month-card">
          <div class="month-name-row">
            <h3 class="month-name">${escapeHtml(monthLabel(key))}</h3>
            <div class="signal-key" aria-label="Calendar signal key">
              <span><i class="key-dot interval"></i>Intervals</span>
              <span><i class="key-dot big"></i>Big: ${BIG_DAY_LABEL}</span>
              <span><i class="key-dot race-road"></i>Road/Crit</span>
              <span><i class="key-dot race-dirt"></i>Gravel/Dirt</span>
            </div>
          </div>
          ${renderMonthSummary(stats)}
          <div class="month-weekdays">
            ${["M", "T", "W", "T", "F", "S", "S"].map((label) => `<div class="weekday">${label}</div>`).join("")}
            <div class="weekday week-total-heading">Week</div>
          </div>
          <div class="month-rows">
            ${rows.map(renderMonthWeekRow).join("")}
          </div>
        </article>`;
    }

    function buildMonthRows(days, leadingBlanks) {
      const slots = [
        ...Array.from({ length: leadingBlanks }, () => null),
        ...days
      ];
      while (slots.length % 7) slots.push(null);
      const rows = [];
      for (let index = 0; index < slots.length; index += 7) {
        rows.push(slots.slice(index, index + 7));
      }
      return rows;
    }

    function renderMonthWeekRow(row) {
      return `
        <div class="month-week-row">
          ${row.map((day) => day ? renderCalendarDay(day) : '<div class="blank-day"></div>').join("")}
          ${renderCalendarWeekStat(row)}
        </div>`;
    }

    function weekStatusForToday(week, today = TODAY) {
      const validDate = (value) => {
        if (typeof value !== "string" || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) return false;
        const time = Date.parse(`${value}T00:00:00Z`);
        return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
      };
      if (!validDate(today) || !validDate(week?.start_date) || !validDate(week?.end_date) || week.end_date < week.start_date) {
        return { status: "budget_missing", label: "Budget not set" };
      }
      const period = today < week.start_date ? "future" : today > week.end_date ? "completed" : "current";
      const variant = week.status_by_period?.[period];
      return variant && typeof variant.status === "string" && typeof variant.label === "string"
        ? variant : { status: week.status || "", label: week.status_label || "Plan loaded" };
    }

    function weekStatusCopy(week, status) {
      const descriptions = {
        budget_missing: "No weekly TSS budget or complete prescribed-session total is set. Hours remain a separate constraint.",
        budget_review: "The coaching plan changed. Review this budget before using it.",
        budget_set: "The week's load budget is set. Provisional targets remain open to coaching review.",
        in_progress: "Recorded load is accumulating against the week's budget. No daily pacing is assumed.",
        load_incomplete: "Some recorded load is missing, so this total cannot establish whether the budget was met.",
        above_ceiling: `Recorded load exceeds the explicit planning ceiling. Review recovery and optional work.${week.tss_partial ? " Some load is still missing." : ""}`,
        above_budget: "Recorded load finished above the intended budget. Review what the week required.",
        below_budget: "Recorded load finished below the intended budget. No catch-up riding is required.",
        within_budget: "Recorded load finished within the intended budget.",
        not_measured: "No comparable recorded-load evidence is available for this week."
      };
      return descriptions[status] || "The plan and recorded activities are loaded for this week.";
    }

    function renderCalendarWeekStat(row) {
      const anchorDay = row.find(Boolean);
      const week = anchorDay ? weekForDate(anchorDay.date) : null;
      if (!week) return '<div class="calendar-week-stat" aria-hidden="true"></div>';
      const energyLabel = week.estimated_tss_label && week.estimated_tss_label !== "-- TSS"
        ? `${week.estimated_tss_label}${week.tss_qualifier ? ` · ${week.tss_qualifier}` : ""}`
        : (week.kilojoules_label || "0 kJ");
      const weekDays = DATA.days.filter((day) => week.start_date <= day.date && day.date <= week.end_date);
      const stats = monthStats(weekDays);
      return `
        <button class="calendar-week-stat" type="button" data-week-start="${escapeHtml(week.start_date)}" title="Open ${escapeHtml(dayLabel(week.start_date))} week">
          <span class="stat-label">${escapeHtml(dayLabel(week.start_date))} week</span>
          <strong>${escapeHtml(week.meaningful_ride_hours_label)}</strong>
          <span class="week-stat-line"><span>${escapeHtml(week.target_hours_label)}</span><span>${escapeHtml(weekStatusForToday(week).label)}</span></span>
          <span>${escapeHtml(energyLabel)}</span>
          <span>${stats.intervalDays} int / ${stats.bigDays} big / ${stats.raceDays} race</span>
        </button>`;
    }

    function renderCalendarDay(day) {
      const planned = truncate(day.planned || "No plan", 58);
      const signals = calendarDaySignals(day);
      const classes = [
        "calendar-day",
        day.date === state.selectedDate ? "selected" : "",
        day.date === TODAY ? "today" : "",
        signals.ride ? "has-ride" : "",
        signals.event ? "has-event" : "",
        (signals.note || signals.coachNote) ? "has-note" : "",
        signals.interval ? "interval-day" : "",
        signals.race ? "race-day" : "",
        signals.raceKind === "road" ? "race-road-day" : "",
        signals.raceKind === "dirt" ? "race-dirt-day" : "",
        signals.big ? "big-day" : ""
      ].filter(Boolean).join(" ");
      return `
        <button type="button" class="${classes}" data-date="${escapeHtml(day.date)}">
          <span class="day-number">${Number(day.date.slice(8, 10))}</span>
          ${renderDayTags(day, signals)}
          <span class="day-mini">${escapeHtml(planned)}</span>
          ${renderDayKpi(day)}
        </button>`;
    }

    function renderMonthSummary(stats) {
      const load = stats.tssDays
        ? `${formatTssNumber(stats.estimatedTss)} TSS`
        : "-- TSS";
      const coverage = stats.tssPowerReportedSeconds > 0 ? stats.tssPowerLoadSeconds / stats.tssPowerReportedSeconds : null;
      const qualifier = [stats.tssDays ? (stats.tssEstimated ? "Calculated" : "Source") : "",
        stats.tssPowerPartial && coverage !== null ? `${formatCoverageNumber(Math.min(99.9, 100 * coverage))}% power coverage` : "",
        stats.tssMissingRides ? `${stats.tssMissingRides} ride${stats.tssMissingRides === 1 ? "" : "s"} without load` : ""
      ].filter(Boolean).join(" · ");
      return `
        <aside class="month-summary-card" aria-label="Month training summary">
          <p class="eyebrow">Month read</p>
          <div class="month-summary-main">
            <strong>${formatNumber(stats.meaningfulHours, 1)}h</strong>
            <span>meaningful bike time</span>
          </div>
          <div class="month-summary-grid">
            <div class="month-summary-stat"><strong>${stats.intervalDays}</strong><span>interval</span></div>
            <div class="month-summary-stat"><strong>${stats.bigDays}</strong><span>big ${BIG_DAY_LABEL}</span></div>
            <div class="month-summary-stat"><strong>${stats.raceDays}</strong><span>race</span></div>
          </div>
          <div class="month-summary-line">
            <strong>${escapeHtml(load)} / ${formatNumber(stats.kilojoules, 0)} kJ</strong>
            ${qualifier ? `<span>${escapeHtml(qualifier)}</span>` : ""}
            <span>${stats.rideDays} ride days / ${stats.noteDays} daily notes / ${stats.coachNoteDays} coach notes</span>
          </div>
        </aside>`;
    }

    function selectedDayIndex() {
      return DATA.days.findIndex((day) => day.date === state.selectedDate);
    }

    function moveDay(delta) {
      const current = selectedDayIndex();
      if (current < 0) return;
      const nextIndex = Math.max(0, Math.min(DATA.days.length - 1, current + delta));
      const day = DATA.days[nextIndex];
      if (day) selectDate(day.date, { switchWeek: true, openRide: true });
    }

    function recoveryScore(day) {
      const recovery = day?.recovery || {};
      const nightHr = Number(recovery.night_hr || recovery.resting_hr || 0);
      const nightStress = Number(recovery.night_stress || recovery.day_stress || 0);
      if (!Number.isFinite(nightHr) || !nightHr || !Number.isFinite(nightStress) || !nightStress) return null;
      const hrScore = Math.max(0, Math.min(42, 42 - Math.max(0, nightHr - 46) * 2.2));
      const stressScore = Math.max(0, Math.min(43, 43 - Math.max(0, nightStress - 8) * 1.6));
      return Math.round(Math.max(35, Math.min(96, 15 + hrScore + stressScore)));
    }

    function coachPresenceRead(day, week) {
      const planned = String(day?.planned || "").toLowerCase();
      const actual = String(day?.actual || "").toLowerCase();
      const score = recoveryScore(day);
      const hasRide = Boolean(day?.has_synced_ride);
      const isRace = /\brace\b|criterium|crit|road race|grasshopper|giro/.test(`${planned} ${actual}`);
      const isHard = /\b(vo2|threshold|over.?under|sweet spot|ss\b|anaerobic|sprint|hard)\b/.test(planned);
      const isLong = Number(day?.planned_load?.hours_max || day?.planned_load?.hours || 0) >= 3.5;
      const isEasy = /\b(rest|off|easy|recovery|z1|zone 1)\b/.test(planned);
      const isWeekend = ["Sat", "Sun"].includes(day?.weekday);
      const weekFocus = week?.primary_focus || day?.week_focus || "the week";
      const block = week?.phase || day?.phase || "build";

      if (hasRide) {
        if (isRace) {
          return {
            eyebrow: "After the race",
            title: "Let the read settle.",
            copy: "Use the result as evidence, not identity. What matters now is what the body is telling you about the work ahead."
          };
        }
        if (isHard) {
          return {
            eyebrow: "After the work",
            title: "Absorb the signal.",
            copy: `The useful question now is not whether it was perfect, but whether it moved ${weekFocus.toLowerCase()} forward without stealing from the next good day.`
          };
        }
        if (isLong || isWeekend) {
          return {
            eyebrow: "After the ride",
            title: "Bank it. Don’t chase it.",
            copy: `That was the deposit. Let the rest of the ${String(block).toLowerCase()} block stay about consistency, not proving the day twice.`
          };
        }
        return {
          eyebrow: "After the ride",
          title: "Keep the thread.",
          copy: `Take the small win for what it is, then keep the rest of the week pointed at ${weekFocus.toLowerCase()}.`
        };
      }

      if (isRace) {
        return {
          eyebrow: "Race-day read",
          title: "Arrive with a point.",
          copy: "Do not spend the day asking if you are ready. Know what you want from the race, then let the race answer back."
        };
      }
      if (score !== null && score < 55 && isHard) {
        return {
          eyebrow: "Watch the body",
          title: "Protect the good work.",
          copy: "The plan can stay ambitious without today becoming stubborn. If the warmup says no, keep the shape and downshift."
        };
      }
      if (isHard) {
        return {
          eyebrow: "Day’s ask",
          title: "Make it count, not heroic.",
          copy: `This is one of the days that gives the ${String(block).toLowerCase()} its shape. Hit the point cleanly and leave something for the next one.`
        };
      }
      if (isLong) {
        return {
          eyebrow: "Day’s ask",
          title: "Let the day get long.",
          copy: `The job is quiet durability. Fuel it, keep it honest, and let the week accumulate around ${weekFocus.toLowerCase()}.`
        };
      }
      if (isEasy) {
        return {
          eyebrow: "Day’s ask",
          title: "Nothing to prove today.",
          copy: `Absorb the block, keep the rhythm, and let the week stay pointed at ${weekFocus.toLowerCase()}.`
        };
      }
      return {
        eyebrow: "Coach read",
        title: "Keep the shape.",
        copy: `Let the day serve the week, and let the week keep pointing at ${weekFocus.toLowerCase()}.`
      };
    }

    function renderCoachPresence(day, week) {
      const eyebrow = document.getElementById("coach-presence-eyebrow");
      const title = document.getElementById("coach-presence-title");
      const copy = document.getElementById("coach-presence-copy");
      if (!eyebrow || !title || !copy || !day) return;
      const read = coachPresenceRead(day, week || {});
      eyebrow.textContent = read.eyebrow;
      title.textContent = read.title;
      copy.textContent = read.copy;
    }

    function plannedIntentLabel(day) {
      const text = String(day?.planned || "").toLowerCase();
      const explicit = String(day?.planned_load?.intensity || "").trim();
      if (explicit) return explicit.replace(/(^|\\s)\\S/g, (match) => match.toUpperCase());
      if (/\b(vo2|anaerobic|sprint|openers?)\b/.test(text)) return "Sharp";
      if (/\b(threshold|over.?under|sweet spot|ss\b)\b/.test(text)) return "Work";
      if (/\b(tempo|z3|zone 3)\b/.test(text)) return "Steady";
      if (/\b(rest|off|recovery|easy|z1|zone 1)\b/.test(text)) return "Absorb";
      if (/\b(z2|zone 2|endurance)\b/.test(text)) return "Aerobic";
      return "Train";
    }

    function plannedTssLabelForDay(day) {
      if (day?.planned_load?.estimated_tss != null) return day.planned_load.tss_value_label || plannedDayTssLabel(day);
      if (day?.planned_load?.hours != null) return `${plannedDayTimeLabel(day)} plan`;
      return "--";
    }

    function plannedSuccessLabel(day) {
      const text = String(day?.planned || "").toLowerCase();
      if (/\b(vo2|anaerobic|sprint|openers?)\b/.test(text)) return "Finish with snap";
      if (/\b(threshold|over.?under|sweet spot|ss\b)\b/.test(text)) return "Clean, not heroic";
      if (/\b(rest|off|recovery|easy|z1|zone 1)\b/.test(text)) return "Feel better after";
      if (/\b(z2|zone 2|endurance|tempo)\b/.test(text)) return "Keep it honest";
      return "Serve the week";
    }

    function renderCoachRailExpandedDetail(day) {
      const activityCards = (day.activities || []).length
        ? day.activities.map(renderActivityCard).join("")
        : '<p class="sidebar-copy">No synced Strava ride for this day.</p>';
      return `
        <section class="sidebar-section">
          <h4>Analysis</h4>
          ${renderSidebarStats(day)}
        </section>
        <section class="sidebar-section">
          <h4>Night + recovery</h4>
          ${renderRecoveryStats(day)}
        </section>
        <section class="sidebar-section">
          <h4>Strava</h4>
          <div class="activity-list">${activityCards}</div>
        </section>`;
    }

    function renderCoachRailNotes(day) {
      return `
        <section class="rail-coach-notes">
          <h4>Coach notes</h4>
          ${renderCoachNotes(day)}
        </section>`;
    }

    function renderCoachRail() {
      const label = document.getElementById("coach-date-label");
      const contextLabel = document.getElementById("coach-day-context-label");
      const content = document.getElementById("coach-rail-content");
      if (!label || !content) return;
      const day = dayByDate(state.selectedDate);
      if (!day) {
        label.textContent = "No day selected";
        content.innerHTML = "";
        return;
      }
      const week = weekForDate(day.date) || {};
      renderCoachPresence(day, week);
      const metrics = day.metrics || {};
      const primary = primaryActivity(day);
      const score = recoveryScore(day);
      const recovery = day.recovery || {};
      const recoveryStatus = score === null
        ? (recovery.available ? "Partial" : "Pending")
        : (score >= 80 ? "High" : score >= 65 ? "Steady" : score >= 50 ? "Watch" : "Low");
      const plannedTssLabel = plannedDayTssLabel(day).replace(/ TSS$/, "");
      const actualTssLabel = dayTssLabel(day, false);
      const sessionSubtitle = day.has_synced_ride
        ? (day.actual_title_from_plan || day.actual === day.planned
          ? `${primary?.source_label || "Local"} recording · workout name from plan`
          : truncate(day.actual || "", 88))
        : truncate(week.primary_focus || day.week_focus || "", 88);
      const expanded = Boolean(content.querySelector(".rail-detail[open]"));
      label.textContent = longDayLabel(day.date);
      if (contextLabel) {
        const anchor = todayAnchorDate();
        contextLabel.textContent = day.date === TODAY
          ? "Today"
          : day.date === anchor && anchor < TODAY
            ? "Latest recorded day"
            : day.date === anchor
              ? "Next scheduled day"
              : "Selected day";
      }
      content.innerHTML = `
        <section class="rail-section">
          <div class="section-title-row">
            <p class="section-title">Day brief</p>
            <span class="phase-chip">${escapeHtml(week.phase || day.phase || "Training")}</span>
          </div>
          <article class="session-card">
            <span class="session-icon" aria-hidden="true">~</span>
            <div class="session-copy">
              <strong>${escapeHtml(day.planned || "No planned session")}</strong>
              ${sessionSubtitle ? `<p>${escapeHtml(sessionSubtitle)}</p>` : ""}
            </div>
            <span class="session-duration">${escapeHtml(day.has_synced_ride ? dayTimeLabel(day) : plannedDayTimeLabel(day))}</span>
          </article>
          ${renderRailSpark(day)}
          <div class="rail-mini-grid">
            <div title="${escapeHtml(day.planned_load?.note || "")}"><strong>${escapeHtml(plannedTssLabel)}</strong><span>plan TSS${day.planned_load?.estimated ? " · forecast" : ""}</span></div>
            <div title="${escapeHtml(metrics.tss_description || "")}"><strong>${escapeHtml(actualTssLabel)}</strong><span>${metrics.tss_estimated ? "calculated" : "recorded"} TSS</span></div>
            <div><strong>${escapeHtml(plannedDayTimeLabel(day))}</strong><span>scheduled time</span></div>
            <div><strong>${escapeHtml(day.has_synced_ride ? dayTimeLabel(day) : "--")}</strong><span>recorded time</span></div>
            <div><strong>${escapeHtml(recoveryStatus)}</strong><span>recovery</span></div>
            <div><strong>${hasDailyNote(day) ? "yes" : "--"}</strong><span>note</span></div>
          </div>
          ${metrics.tss_partial || metrics.tss_missing_activity_count ? `<p class="rail-load-note" title="${escapeHtml(metrics.tss_description || "")}">${escapeHtml(metrics.tss_qualifier || "Load data incomplete")}</p>` : ""}
        </section>
        ${renderDailyNoteEditor(day, { id: "coach-rail-note" })}
        ${renderCoachRailNotes(day)}
        <details class="rail-detail"${expanded ? " open" : ""}>
          <summary class="rail-detail-summary">
            <span class="rail-detail-cue">
              <span class="collapsed">More detail</span>
              <span class="expanded">Less detail</span>
            </span>
          </summary>
          <div class="rail-detail-body">
            ${renderCoachRailExpandedDetail(day)}
          </div>
        </details>`;
      bindCoachRailNote();
      const previous = document.getElementById("previous-day");
      const todayButton = document.getElementById("jump-to-today");
      const next = document.getElementById("next-day");
      const index = selectedDayIndex();
      if (previous) previous.disabled = index <= 0;
      if (todayButton) {
        const anchor = todayAnchorDate();
        const anchorLabel = todayAnchorLabel().toLowerCase();
        todayButton.disabled = day.date === anchor || !dayByDate(anchor);
        todayButton.title = `Jump to ${anchorLabel} day`;
        todayButton.setAttribute("aria-label", `Jump to ${anchorLabel} day`);
      }
      if (next) next.disabled = index < 0 || index >= DATA.days.length - 1;
    }

    function bindCoachRailNote() {
      const textarea = document.getElementById("coach-rail-note");
      if (!textarea) return;
      textarea.addEventListener("input", () => {
        updateNote(textarea.dataset.inlineDate, textarea.value);
        syncNoteInputs(textarea.dataset.inlineDate, textarea.value, textarea);
      });
    }

    function normalizedPoints(values, width, height, pad = 8) {
      const clean = values.map((value) => Number(value || 0));
      const max = Math.max(1, ...clean);
      const min = Math.min(0, ...clean);
      const spread = Math.max(1, max - min);
      const step = clean.length > 1 ? (width - pad * 2) / (clean.length - 1) : 0;
      return clean.map((value, index) => {
        const x = pad + step * index;
        const y = height - pad - ((value - min) / spread) * (height - pad * 2);
        return [Number(x.toFixed(1)), Number(y.toFixed(1))];
      });
    }

    function rangedPoints(values, width, height, minRange, pad = 8) {
      const clean = values.map((value) => Number(value)).filter((value) => Number.isFinite(value));
      if (!clean.length) return [];
      const min = Math.min(...clean);
      const max = Math.max(...clean);
      const spread = Math.max(1, minRange, max - min);
      const step = clean.length > 1 ? (width - pad * 2) / (clean.length - 1) : 0;
      return clean.map((value, index) => {
        const x = pad + step * index;
        const y = height - pad - ((value - min) / spread) * (height - pad * 2);
        return [Number(x.toFixed(1)), Number(y.toFixed(1))];
      });
    }

    function pointString(points) {
      return points.map(([x, y]) => `${x},${y}`).join(" ");
    }

    function areaString(points, height, pad = 8) {
      if (!points.length) return "";
      return `${pointString(points)} ${points[points.length - 1][0]},${height - pad} ${points[0][0]},${height - pad}`;
    }

    function cumulative(values) {
      let sum = 0;
      let complete = true;
      return values.map((value) => {
        if (typeof value !== "number" || !Number.isFinite(value) || value < 0) complete = false;
        if (!complete) return null;
        sum += value;
        return Number(sum.toFixed(1));
      });
    }

    function renderLoadSvg(actualValues, options = {}) {
      const width = Number(options.width || 620);
      const height = Number(options.height || 138);
      const pad = Number(options.pad || 12);
      const plannedValues = Array.isArray(options.plannedValues) ? options.plannedValues : [];
      const totalPoints = Math.max(1, Number(options.totalPoints || plannedValues.length || actualValues.length || 1));
      const numeric = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
      const max = Math.max(1, ...actualValues.filter(numeric), ...plannedValues.filter(numeric));
      const normalize = (values) => {
        const step = totalPoints > 1 ? (width - pad * 2) / (totalPoints - 1) : 0;
        return values.map((value, index) => {
          if (!numeric(value)) return null;
          const x = pad + step * index;
          const y = height - pad - (value / max) * (height - pad * 2);
          return [Number(x.toFixed(1)), Number(y.toFixed(1))];
        });
      };
      const runs = (points) => {
        const result = [];
        let current = [];
        for (const point of points) {
          if (point) current.push(point);
          else if (current.length) { result.push(current); current = []; }
        }
        if (current.length) result.push(current);
        return result;
      };
      const actual = runs(normalize(actualValues));
      const planned = runs(normalize(plannedValues));
      const last = actual.at(-1)?.at(-1);
      return `
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Cumulative recorded and planned TSS chart">
          <line class="grid-line" x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}"></line>
          <line class="grid-line" x1="${pad}" y1="${Math.round(height / 2)}" x2="${width - pad}" y2="${Math.round(height / 2)}"></line>
          ${actual.map((run) => `<polygon class="actual-area" points="${areaString(run, height, pad)}"></polygon>`).join("")}
          ${planned.map((run) => `<polyline class="planned-line" points="${pointString(run)}"></polyline>`).join("")}
          ${actual.map((run) => `<polyline class="actual-line" points="${pointString(run)}"></polyline>`).join("")}
          ${last ? `<circle class="chart-dot" cx="${last[0]}" cy="${last[1]}" r="4"></circle>` : ""}
        </svg>`;
    }

    function weekLoadSeries(week) {
      const days = week?.days || [];
      const numeric = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
      const hasRecordings = days.some((day) => day.date <= TODAY && (
        day.has_synced_ride || Number(day.metrics?.activity_count || 0) > 0 || numeric(day.metrics?.estimated_tss)
      ));
      let unsupported = false;
      let partialPower = false;
      const recorded = days.map((day) => {
        if (day.date > TODAY) return null;
        const metrics = day.metrics || {};
        partialPower ||= Boolean(metrics.tss_power_incomplete);
        if (Number(metrics.tss_missing_activity_count || 0) > 0) { unsupported = true; return null; }
        if (numeric(metrics.estimated_tss)) return metrics.estimated_tss;
        if (metrics.tss_missing_activity_count === 0) return 0;
        if (day.has_synced_ride || Number(metrics.activity_count || 0) > 0) { unsupported = true; return null; }
        return 0;
      });
      const planned = cumulative(days.map((day) => day.planned_load?.estimated_tss));
      const notes = [];
      if (planned.some((value) => value === null)) notes.push("The planned curve ends where daily load is unspecified.");
      if (unsupported) notes.push("The recorded curve ends at a ride with unsupported load.");
      if (partialPower) notes.push(`Recorded load includes available power only${week.tss_qualifier ? `: ${week.tss_qualifier}` : ""}.`);
      else if (hasRecordings && week.tss_estimated) notes.push("Recorded TSS is calculated using your current FTP.");
      return { actual: hasRecordings ? cumulative(recorded) : [], planned, totalPoints: days.length, note: notes.join(" ") };
    }

    function powerZoneClass(watts) {
      const value = Number(watts || 0);
      const ftp = Number(POWER_ZONE_FTP || 0);
      if (!Number.isFinite(value) || !Number.isFinite(ftp) || value <= 0 || ftp <= 0) return "zone-hr";
      const fraction = value / ftp;
      if (fraction <= 0.55) return "zone-z1";
      if (fraction <= 0.75) return "zone-z2";
      if (fraction <= 0.9) return "zone-z3";
      if (fraction <= 1.05) return "zone-z4";
      if (fraction <= 1.2) return "zone-z5";
      return "zone-z6";
    }

    function actualIntervalBarSegments(activity) {
      const laps = activity?.laps || [];
      if (laps.length < 2) return [];
      const labels = (activity?.interval_labels || []).join(" ");
      const intervalish = /\\b(interval|vo2|threshold|sweet spot|over.?under|z4|z5|anaerobic|sprint)\\b/i.test(labels) ||
        /\\b(interval|vo2|threshold|sweet spot)\\b/i.test(activity?.ride_profile_label || "");
      if (!intervalish) return [];
      const bars = laps
        .map((lap) => {
          const watts = Number(lap.np_watts || lap.avg_watts || 0);
          const value = watts || Number(lap.avg_hr || 0);
          return {
            value,
            duration: Math.max(30, Number(lap.moving_time_s || 0)),
            zoneClass: watts ? powerZoneClass(watts) : "zone-hr"
          };
        })
        .filter((bar) => Number.isFinite(bar.value) && bar.value > 0);
      if (bars.length < 2) return [];
      const maxValue = Math.max(1, ...bars.map((bar) => bar.value));
      const maxDuration = Math.max(30, ...bars.map((bar) => bar.duration));
      return bars.slice(0, 12).map((bar) => ({
        height: Math.max(10, Math.round((bar.value / maxValue) * 100)),
        width: Math.max(0.45, Math.min(2.2, bar.duration / maxDuration)),
        zoneClass: bar.zoneClass
      }));
    }

    function planDurationSeconds(value, unit) {
      const amount = Number(value || 0);
      if (!Number.isFinite(amount) || amount <= 0) return 0;
      return String(unit || "").toLowerCase().startsWith("s") ? amount : amount * 60;
    }

    function plannedPrimaryText(day) {
      return String(day?.planned || "")
        .replace(/[–—]/g, "-")
        .replace(/×/g, "x")
        .split(/\\bif wet\\b/i)[0]
        .split(/\\s+or\\s+/i)[0]
        .trim();
    }

    function plannedZoneClass(text, fallback = "zone-z2") {
      const source = String(text || "").toLowerCase();
      if (/\\b(sprints?|anaerobic|opener|openers|fast|poppers?)\\b/.test(source)) return "zone-z6";
      if (/\\b(vo2|hard)\\b/.test(source)) return "zone-z5";
      if (/\\b(threshold|over.?under|z4|zone 4|sweet spot|low-ss|ss\\b)\\b/.test(source)) return "zone-z4";
      if (/\\b(tempo|z3|zone 3)\\b/.test(source)) return "zone-z3";
      if (/\\b(recovery|very easy|easy spin|rest)\\b/.test(source)) return "zone-z1";
      if (/\\b(z1|zone 1)\\b/.test(source)) return "zone-z1";
      if (/\\b(z2|zone 2|endurance|easy)\\b/.test(source)) return "zone-z2";
      return fallback;
    }

    function plannedZoneHeight(zoneClass) {
      return ({
        "zone-z1": 20,
        "zone-z2": 34,
        "zone-z3": 52,
        "zone-z4": 70,
        "zone-z5": 86,
        "zone-z6": 100
      })[zoneClass] || 34;
    }

    function plannedRideSeconds(day) {
      const hours = Number(day?.planned_load?.hours || 0);
      return Number.isFinite(hours) && hours > 0 ? hours * 3600 : 0;
    }

    function plannedRecoverySeconds(text, blockEnd, workSeconds) {
      const tail = text.slice(blockEnd, blockEnd + 80);
      const explicit = tail.match(
        /(?:\\/|with|w\\/)\\s*(\\d+(?:\\.\\d+)?)(?:\\s*-\\s*(\\d+(?:\\.\\d+)?))?\\s*(sec|secs|second|seconds|s|min|mins|minute|minutes|m)\\s*(?:easy|recovery|rest)\\b/i
      );
      if (explicit) {
        return planDurationSeconds(explicit[2] || explicit[1], explicit[3]);
      }
      if (/\\bfull\\s+(?:recovery|rest)\\b/i.test(tail)) return workSeconds;
      return 0;
    }

    function plannedDefaultRecoverySeconds(workSeconds, workZone, reps) {
      if (reps <= 1) return 0;
      if (workZone === "zone-z6") return Math.max(60, Math.min(120, workSeconds * 6));
      if (workZone === "zone-z5") return workSeconds;
      if (workZone === "zone-z4") return Math.max(120, Math.min(300, workSeconds * 0.2));
      if (workZone === "zone-z3") return Math.max(90, Math.min(240, workSeconds * 0.15));
      return 0;
    }

    function plannedIntervalBarSegments(day) {
      const text = plannedPrimaryText(day);
      if (!text) return [];

      const overallZone = plannedZoneClass(
        `${text} ${day?.planned_load?.intensity || ""}`,
        "zone-z2"
      );
      const baselineZone = overallZone === "zone-z1" ? "zone-z1" : "zone-z2";
      const rawSegments = [];
      const blockPattern = /\\b(\\d+)(?:\\s*-\\s*(\\d+))?\\s*x\\s*(\\d+(?:\\.\\d+)?)(?:\\s*-\\s*(\\d+(?:\\.\\d+)?))?\\s*(sec|secs|second|seconds|s|min|mins|minute|minutes|m)\\b/gi;

      for (const match of text.matchAll(blockPattern)) {
        const reps = Math.max(1, Math.min(12, Number(match[2] || match[1] || 0)));
        const workSeconds = planDurationSeconds(match[4] || match[3], match[5]);
        if (!workSeconds) continue;
        const context = text.slice(Math.max(0, match.index - 24), match.index + match[0].length + 48);
        const workZone = plannedZoneClass(context, overallZone);
        const recoverySeconds = plannedRecoverySeconds(text, match.index + match[0].length, workSeconds) ||
          plannedDefaultRecoverySeconds(workSeconds, workZone, reps);
        for (let index = 0; index < reps; index += 1) {
          rawSegments.push({ duration: workSeconds, zoneClass: workZone });
          if (recoverySeconds && index < reps - 1) {
            rawSegments.push({ duration: recoverySeconds, zoneClass: "zone-z1" });
          }
        }
      }

      if (!rawSegments.length) {
        const rideSeconds = plannedRideSeconds(day);
        if (!rideSeconds || overallZone === "zone-z1") return [];
        rawSegments.push({ duration: rideSeconds, zoneClass: overallZone });
      }

      const prescribedSeconds = rawSegments.reduce((sum, segment) => sum + segment.duration, 0);
      const rideSeconds = plannedRideSeconds(day);
      const remainingSeconds = rideSeconds > prescribedSeconds ? rideSeconds - prescribedSeconds : 0;
      if (remainingSeconds >= 120) {
        const leadInSeconds = Math.round(remainingSeconds * 0.45);
        const coolDownSeconds = remainingSeconds - leadInSeconds;
        rawSegments.unshift({ duration: leadInSeconds, zoneClass: baselineZone });
        rawSegments.push({ duration: coolDownSeconds, zoneClass: baselineZone });
      }

      const maxDuration = Math.max(1, ...rawSegments.map((segment) => segment.duration));
      return rawSegments.slice(0, 28).map((segment) => ({
        height: plannedZoneHeight(segment.zoneClass),
        width: Math.max(0.32, Math.min(2.4, segment.duration / maxDuration)),
        zoneClass: segment.zoneClass
      }));
    }

    function renderBarSpark(segments, label = "laps", tone = "actual") {
      if (!segments.length) return "";
      return `
        <div class="day-spark bars ${escapeHtml(tone)}" aria-label="${escapeHtml(tone === "planned" ? "Planned interval bars" : "Actual interval lap bars")}">
          <span class="spark-tag">${escapeHtml(label)}</span>
          <div class="spark-bars">
            ${segments.map((segment) => `<i class="${escapeHtml(segment.zoneClass || "zone-hr")}" style="--h: ${segment.height}%; --w: ${segment.width}"></i>`).join("")}
          </div>
        </div>`;
    }

    function graphActivity(day) {
      const activities = day.activities || [];
      const rides = activities.filter((activity) => {
        return /ride/i.test(String(activity?.sport || "")) && Number(activity?.estimated_tss || 0) > 0;
      });
      if (!rides.length) return primaryActivity(day);
      return rides.reduce((best, activity) => {
        return Number(activity.estimated_tss || 0) > Number(best.estimated_tss || 0) ? activity : best;
      });
    }

    function dayGraphValues(day) {
      const graph = graphActivity(day);
      const shape = graph?.stream_shape || {};
      const values = Array.isArray(shape.values) ? shape.values : [];
      return values.map((value) => Number(value || 0)).filter((value) => Number.isFinite(value) && value >= 0);
    }

    function dayElevationValues(day) {
      const graph = graphActivity(day);
      const shape = graph?.stream_shape || {};
      const values = Array.isArray(shape.elevation_ft) ? shape.elevation_ft : [];
      return values.map((value) => Number(value)).filter((value) => Number.isFinite(value));
    }

    function sparkSegments(points, values, source, width) {
      if (!points.length) return "";
      return points.map((point, index) => {
        const left = index === 0
          ? Math.max(0, point[0] - 1.6)
          : Number((((points[index - 1][0] + point[0]) / 2) + 0.4).toFixed(1));
        const right = index === points.length - 1
          ? Math.min(width, point[0] + 1.6)
          : Number((((point[0] + points[index + 1][0]) / 2) - 0.4).toFixed(1));
        const zoneClass = source === "watts" ? powerZoneClass(values[index]) : "zone-hr";
        return `<line class="spark-segment ${zoneClass}" x1="${left}" x2="${right}" y1="${point[1]}" y2="${point[1]}"></line>`;
      }).join("");
    }

    function renderDaySpark(day, options = {}) {
      const graph = graphActivity(day);
      const values = dayGraphValues(day);
      if (!values.length && !day.has_synced_ride) {
        const plannedBars = plannedIntervalBarSegments(day);
        if (plannedBars.length) return renderBarSpark(plannedBars, "planned effort", "planned");
      }
      if (!values.length) return '<div class="day-spark empty" aria-hidden="true"></div>';
      const expanded = Boolean(options.expanded);
      const width = Number(options.width || 116);
      const height = Number(options.height || 41);
      const maxWidth = expanded ? width : Math.min(560, Math.max(240, values.length * 6));
      const points = normalizedPoints(values, width, height, 3);
      const elevationPoints = rangedPoints(dayElevationValues(day), width, height, 200, 3);
      const source = graph?.stream_shape?.source === "heartrate" ? "hr" : "watts";
      const streamLabel = graph?.stream_shape?.label || `${source === "hr" ? "HR" : "Power"} stream`;
      const segments = sparkSegments(points, values, source, width);
      const background = elevationPoints.length
        ? `
            <polygon class="spark-elevation-fill" points="${areaString(elevationPoints, height, 3)}"></polygon>
            <polyline class="spark-elevation-line" points="${pointString(elevationPoints)}"></polyline>`
        : `<polygon class="spark-fill" points="${areaString(points, height, 3)}"></polygon>`;
      return `
        <div class="day-spark${expanded ? " expanded-spark" : ""}" style="--spark-max-width: ${maxWidth}px" aria-label="${escapeHtml(streamLabel)}">
          ${source === "hr" ? `<span class="spark-tag">${escapeHtml(source)}</span>` : ""}
          <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
            ${background}
            ${segments}
          </svg>
        </div>`;
    }

    function renderRailSpark(day) {
      const compact = renderDaySpark(day);
      if (!dayGraphValues(day).length) return compact;
      const graph = graphActivity(day);
      const graphLabel = graph?.stream_shape?.label || "Ride trace";
      return `
        <button class="rail-spark-trigger" type="button" popovertarget="rail-graph-popover" aria-haspopup="dialog" aria-label="Expand ${escapeHtml(graphLabel)}">
          ${compact}
          <span class="rail-spark-expand">Expand</span>
        </button>
        <div class="rail-spark-popover" id="rail-graph-popover" popover>
          <div class="rail-spark-popover-head">
            <div>
              <p class="eyebrow">${escapeHtml(dayLabel(day.date))} ride trace</p>
              <h3>${escapeHtml(day.actual || day.planned || "Ride shape")}</h3>
            </div>
            <button class="rail-spark-popover-close" type="button" popovertarget="rail-graph-popover" popovertargetaction="hide">Close</button>
          </div>
          ${renderDaySpark(day, { expanded: true, width: 760, height: 214 })}
          <p class="rail-spark-caption">Effort trace over the elevation profile. Color indicates riding intensity.</p>
        </div>`;
    }

    function renderTodayDashboard() {
      const root = document.getElementById("today-dashboard");
      const meta = document.getElementById("today-meta");
      const viewLabel = document.getElementById("today-view-label");
      const datePicker = document.getElementById("today-date-picker");
      const day = dayByDate(state.selectedDate);
      if (!root || !day) return;
      const week = weekForDate(day.date) || {};
      const primary = primaryActivity(day);
      const series = weekLoadSeries(week);
      const hasActualRide = Boolean(day.has_synced_ride);
      const isToday = day.date === TODAY;
      if (viewLabel) viewLabel.textContent = isToday ? "Today" : "Selected day";
      if (meta) meta.textContent = `${day.weekday} ${longDayLabel(day.date)} / ${week.phase || day.phase || "Training"}`;
      if (datePicker) {
        datePicker.value = day.date;
        datePicker.min = DATA.days[0]?.date || "";
        datePicker.max = DATA.days.at(-1)?.date || "";
      }
      const todayRightCard = hasActualRide
        ? `
          <section class="today-card">
            <div>
              <p class="section-title">Actual <span>from Strava</span></p>
              <h4>${escapeHtml(day.actual || "No synced ride yet.")}</h4>
              <p class="sidebar-copy">${escapeHtml(dayMetricLine(day))}</p>
            </div>
            ${renderDaySpark(day)}
            ${rideAssessmentStats(day)}
            ${renderDailyNoteEditor(day, { id: "today-note", className: "rail-note-label today-note-card" })}
          </section>`
        : `
          <section class="today-card today-context-card">
            <div class="today-context-stack">
              <div class="today-context-block">
                <span class="today-context-number">01</span>
                <p class="section-title">Why this day</p>
                <h4>${escapeHtml(day.planned || "Let the day stay simple.")}</h4>
                <p>${escapeHtml(week.why_logic || week.primary_focus || day.week_focus || "The point is to let the day serve the larger shape of the week.")}</p>
              </div>
              <div class="today-context-block">
                <span class="today-context-number">02</span>
                <p class="section-title">Coach's guardrail</p>
                <h4>${escapeHtml(coachPresenceRead(day, week).title)}</h4>
                <p>${escapeHtml(coachPresenceRead(day, week).copy)}</p>
              </div>
              <div class="today-context-block">
                <span class="today-context-number">03</span>
                <p class="section-title">What would change it</p>
                <h4>Use the warmup as evidence.</h4>
                <p>${escapeHtml(week.execution_note || "If the body is clearly arguing with the plan, keep the intention and change the dose. The week matters more than the script.")}</p>
              </div>
            </div>
            ${renderDailyNoteEditor(day, { id: "today-note", className: "rail-note-label today-note-card" })}
          </section>`;

      root.innerHTML = `
        <section class="today-card primary">
          <div class="today-card-head">
            <div>
              <p class="eyebrow">${isToday ? "Today decision" : "Day decision"}</p>
              <h3>${escapeHtml(day.planned || "No planned session")}</h3>
            </div>
            <span class="phase-chip">${escapeHtml(week.phase || day.phase || "Training")}</span>
          </div>
          ${renderDayTags(day, calendarDaySignals(day))}
          <p class="today-plan-copy">${escapeHtml(week.primary_focus || day.week_focus || "Keep the work matched to today's readiness.")}</p>
          <div class="today-grid">
            ${hasActualRide
              ? `
                ${summaryCard(dayTimeLabel(day), "Ride time")}
                ${summaryCard(dayTssLabel(day), "Training load", dayLoadOptions(day))}
                ${summaryCard(primary?.np_label || "-- NP", "Primary ride NP", { description: primary?.np_description })}`
              : `
                ${summaryCard(plannedTssLabelForDay(day), "Planned load")}
                ${summaryCard(plannedIntentLabel(day), "Intended feel")}
                ${summaryCard(plannedSuccessLabel(day), "Success today")}`
            }
          </div>
          <section class="today-load-card" aria-label="Selected week load chart">
            <div class="week-load-chart-head">
              <h4>Week TSS</h4>
              <div class="chart-legend">
                <span><i></i>Recorded</span>
                <span class="planned"><i></i>Plan / forecast</span>
              </div>
            </div>
            ${renderLoadSvg(series.actual, { height: 150, plannedValues: series.planned, totalPoints: series.totalPoints })}
            ${series.note ? `<p class="week-load-note">${escapeHtml(series.note)}</p>` : ""}
          </section>
        </section>
        ${todayRightCard}`;
      const note = document.getElementById("today-note");
      if (note) {
        note.addEventListener("input", () => {
          updateNote(note.dataset.inlineDate, note.value);
          syncNoteInputs(note.dataset.inlineDate, note.value, note);
        });
      }
    }

    function centerSelectedMonthDay() {
      const stripScroller = document.querySelector("#month-rail .month-strip-scroll");
      const selectedStripDay = document.querySelector("#month-rail .month-strip-day.selected");
      if (!stripScroller || !selectedStripDay || stripScroller.clientWidth <= 0) return;
      if (stripScroller.scrollWidth <= stripScroller.clientWidth) return;
      stripScroller.scrollLeft = Math.max(
        0,
        selectedStripDay.offsetLeft - ((stripScroller.clientWidth - selectedStripDay.offsetWidth) / 2)
      );
    }

    function renderMonthRail() {
      const root = document.getElementById("month-rail");
      const selected = dayByDate(state.selectedDate);
      if (!root || !selected) return;
      const key = monthKey(selected.date);
      const days = DATA.days.filter((day) => monthKey(day.date) === key);
      const upcomingEvents = DATA.days
        .flatMap((day) => (day.events || []).filter((event) => !eventIsSkipped(event)).map((event) => ({ day, event })))
        .filter(({ day }) => day.date >= selected.date)
        .slice(0, 3);
      root.innerHTML = `
        <div class="month-rail-head">
          <div>
            <p class="eyebrow">${escapeHtml(monthLabel(key))}</p>
            <h2>Next context</h2>
          </div>
          <div class="race-marker-list">
            ${upcomingEvents.length
              ? upcomingEvents.map(({ day, event }) => `
                  <button class="race-marker" type="button" data-month-date="${escapeHtml(day.date)}">
                    <span>${escapeHtml(dayLabel(day.date))}</span>
                    <strong>${escapeHtml(event.name || "Race")}</strong>
                    <small>${escapeHtml(event.discipline || event.raw || "")}</small>
                  </button>`).join("")
              : '<span class="quiet-context">No upcoming events in the current plan window</span>'}
          </div>
        </div>
        <div class="month-strip-shell">
          <div class="month-strip-meta">
            <span>Month rhythm</span>
            <span class="month-strip-legend" aria-label="Activity key">
              <span><i></i>Ride</span>
              <span><i class="quality"></i>Quality</span>
              <span><i class="race"></i>Race</span>
            </span>
          </div>
          <div class="month-strip-scroll">
            <div class="month-strip-days" style="--month-day-count: ${days.length}">
              ${days.map((day) => {
                const signals = calendarDaySignals(day);
                const classes = [
                  "month-strip-day",
                  day.date === state.selectedDate ? "selected" : "",
                  day.date === TODAY ? "today" : "",
                  days.length > 7 && ["Sat", "Sun"].includes(day.weekday) ? "weekend" : "",
                  signals.ride ? "has-ride" : "",
                  signals.interval ? "interval" : "",
                  signals.race ? "race" : "",
                  signals.raceKind === "dirt" ? "dirt" : ""
                ].filter(Boolean).join(" ");
                return `
                  <button class="${classes}" type="button" data-month-date="${escapeHtml(day.date)}" title="${escapeHtml(dayLabel(day.date))}: ${escapeHtml(day.planned || "No plan")}">
                    <span>${Number(day.date.slice(8, 10))}</span>
                    <i></i>
                  </button>`;
              }).join("")}
            </div>
          </div>
        </div>`;
      root.querySelectorAll("[data-month-date]").forEach((button) => {
        button.addEventListener("click", () => selectDate(button.dataset.monthDate, { switchWeek: true, openRide: true }));
      });
      centerSelectedMonthDay();
    }

    function phaseTone(phaseName) {
      const name = String(phaseName || "").toLowerCase();
      if (/race|checkpoint|taper/.test(name)) return "race";
      if (/recovery|rest|deload|reset|illness/.test(name)) return "recover";
      if (/build|ftp|crit|climb|threshold|vo2/.test(name)) return "build";
      return "base";
    }

    function seasonLoadSeries(weeks, layout, today) {
      const dayMs = 86400000;
      const dateTime = (value) => {
        const text = String(value || "");
        if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(text)) return NaN;
        const time = Date.parse(`${text}T00:00:00Z`);
        return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === text ? time : NaN;
      };
      const numeric = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
      const spanStart = dateTime(layout?.start_date);
      const spanEnd = dateTime(layout?.end_date) + dayMs;
      const todayTime = dateTime(today);
      if (!Number.isFinite(spanStart) || !Number.isFinite(spanEnd) || spanEnd <= spanStart) {
        return { rows: [], target_runs: [], trajectory_runs: [], recorded_runs: [], max_tss: 100 };
      }
      const percent = (time) => 100 * (time - spanStart) / (spanEnd - spanStart);
      const rows = (Array.isArray(weeks) ? weeks : []).map((week) => {
        const start = dateTime(week?.start_date);
        const end = dateTime(week?.end_date) + dayMs;
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end <= spanStart || start >= spanEnd) return null;
        const plan = week.planned_load || {};
        const legacyHoursForecast = ["weekly_hours_budget", "session_if_forecast"].includes(plan.tss_source)
          || (plan.tss_source === "complete_daily_sum" && plan.estimated === true);
        let targetMin = numeric(plan.estimated_tss_min);
        let targetMax = numeric(plan.estimated_tss_max);
        if (plan.estimated_tss_min == null && plan.estimated_tss_max == null) {
          targetMin = targetMax = numeric(plan.estimated_tss);
        }
        if (targetMin === null || targetMax === null || targetMax < targetMin) targetMin = targetMax = null;
        let targetValue = plan.estimated_tss == null && targetMin !== null
          ? (targetMin + targetMax) / 2 : numeric(plan.estimated_tss);
        if (targetMin === null || targetValue === null || targetValue < targetMin || targetValue > targetMax) targetValue = null;
        if (legacyHoursForecast) targetMin = targetMax = targetValue = null;
        const actual = numeric(week.totals?.estimated_tss);
        const future = !Number.isFinite(todayTime) || start > todayTime;
        const clippedStart = Math.max(start, spanStart);
        const clippedEnd = Math.min(end, spanEnd);
        return {
          start_date: week.start_date, end_date: week.end_date,
          start_ms: clippedStart, end_ms: clippedEnd,
          left: percent(clippedStart), right: percent(clippedEnd),
          center: percent((clippedStart + clippedEnd) / 2),
          target_min: targetMin, target_max: targetMax, target_value: targetValue,
          target_qualifier: String(plan.qualifier || (plan.estimated ? "Forecast" : "")),
          target_source: String(plan.tss_source || ""), target_estimated: plan.estimated === true,
          target_note: String(plan.note || ""),
          target_status: String(plan.budget_status || ""),
          target_ceiling: numeric(plan.budget_ceiling_tss),
          target_review_required: plan.budget_review_required === true,
          recorded_tss: future ? null : actual,
          recorded_qualifier: String(week.tss_qualifier || ""),
          recorded_partial: week.tss_partial === true,
          to_date: Number.isFinite(todayTime) && start <= todayTime && todayTime < end,
          future
        };
      }).filter(Boolean).sort((left, right) => left.start_ms - right.start_ms || left.end_ms - right.end_ms);
      const runs = (key) => {
        const result = [];
        let current = [];
        for (const row of rows) {
          if (row[key] === null) {
            if (current.length) result.push(current);
            current = [];
            continue;
          }
          if (current.length && current.at(-1).end_ms !== row.start_ms) {
            result.push(current);
            current = [];
          }
          current.push(row);
        }
        if (current.length) result.push(current);
        return result;
      };
      const maximum = rows.reduce((value, row) => Math.max(value, row.target_max || 0, row.recorded_tss || 0), 0);
      const tick = maximum <= 200 ? 50 : maximum <= 1000 ? 100 : maximum <= 2500 ? 250 : 500;
      const ceiling = Math.ceil(maximum / tick) * tick;
      return {
        rows, target_runs: runs("target_min"), trajectory_runs: runs("target_value"), recorded_runs: runs("recorded_tss"),
        max_tss: Math.max(100, Number.isFinite(ceiling) ? ceiling : maximum)
      };
    }

    function seasonTss(value) {
      return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
    }

    function seasonWeekLoadLabel(row) {
      if (!row) return "No weekly TSS data";
      const qualify = (label, qualifier) => qualifier ? `${label} (${qualifier})` : label;
      const targetLow = row.target_min === null ? null : seasonTss(row.target_min);
      const targetHigh = row.target_max === null ? null : seasonTss(row.target_max);
      const target = row.target_min === null ? (row.target_review_required ? "TSS budget needs review" : "TSS budget not set")
        : qualify(targetLow === targetHigh ? `Planned ${targetLow} TSS`
          : `Planned ${targetLow}–${targetHigh} TSS`, row.target_qualifier);
      const actual = row.recorded_tss === null
        ? row.future ? "Not recorded yet" : qualify("No supported recorded TSS", row.recorded_qualifier)
        : qualify(`Recorded ${seasonTss(row.recorded_tss)} TSS${row.to_date ? " so far" : ""}`, row.recorded_qualifier);
      const center = row.target_value !== null && row.target_min !== row.target_max
        ? ` · central estimate ${seasonTss(row.target_value)} TSS` : "";
      const ceiling = row.target_min != null && typeof row.target_ceiling === "number" && Number.isFinite(row.target_ceiling)
        ? ` · planning ceiling ${seasonTss(row.target_ceiling)} TSS` : "";
      return `${target}${center}${ceiling} · ${actual}`;
    }

    function seasonLoadProvenance(series) {
      const planned = series.rows.filter((row) => row.target_min !== null);
      const budgets = planned.filter((row) => row.target_source === "coach_budget");
      const prescribed = planned.filter((row) => ["complete_prescribed_sum", "complete_daily_sum", "structured_power_model", "structured_workout_sum"].includes(row.target_source));
      const sources = planned.filter((row) => !budgets.includes(row) && !prescribed.includes(row));
      const recorded = series.rows.filter((row) => row.recorded_tss !== null);
      return {
        planned: planned.length, source: sources.length,
        prescribed: prescribed.length, budget: budgets.length,
        provisional: budgets.filter((row) => row.target_status === "provisional").length,
        missing: series.rows.length - planned.length, recorded: recorded.length,
        incomplete: recorded.filter((row) => row.recorded_partial).length,
        note: "The line follows coach budgets, source targets, or complete prescribed-session totals. Shading shows an intentional target range; missing budgets remain gaps. TSS is training load, not measured fitness."
      };
    }

    function renderSeasonLoadChart(series) {
      const width = 1000;
      const baseline = 108;
      const top = 5;
      const x = (percent) => Number((percent * width / 100).toFixed(2));
      const y = (tss) => Number((baseline - tss / series.max_tss * (baseline - top)).toFixed(2));
      const points = (run, key) => [
        [x(run[0].left), y(run[0][key])],
        ...run.map((row) => [x(row.center), y(row[key])]),
        [x(run.at(-1).right), y(run.at(-1)[key])]
      ];
      const path = (values) => values.map(([px, py], index) => `${index ? "L" : "M"}${px},${py}`).join(" ");
      const targets = series.target_runs.map((run) => {
        const high = points(run, "target_max");
        const low = points(run, "target_min");
        return `<path class="season-target-band" d="${path(high)} ${path(low.slice().reverse()).replace(/^M/, "L")} Z"></path>`;
      }).join("");
      const plannedArea = series.trajectory_runs.map((run) => {
        const values = points(run, "target_value");
        return `<path class="season-planned-area" d="M${values[0][0]},${baseline} ${path(values).replace(/^M/, "L")} L${values.at(-1)[0]},${baseline} Z"></path>`;
      }).join("");
      const trajectory = series.trajectory_runs.map((run) =>
        `<path class="season-target-line" d="${path(points(run, "target_value"))}"></path>`
      ).join("");
      const recorded = series.recorded_runs.map((run) => {
        const values = points(run, "recorded_tss");
        return `<path class="season-recorded-area" d="M${values[0][0]},${baseline} ${path(values).replace(/^M/, "L")} L${values.at(-1)[0]},${baseline} Z"></path><path class="season-recorded-line" d="${path(values)}"></path>`;
      }).join("");
      const hasData = series.target_runs.length || series.recorded_runs.length;
      return `<svg class="season-load-chart" viewBox="0 0 ${width} 112" preserveAspectRatio="none" aria-hidden="true">
        <title>Weekly TSS: planned budget, intentional range, and recorded load</title>
        <line class="season-chart-grid" x1="0" x2="${width}" y1="${baseline}" y2="${baseline}"></line>
        <line class="season-chart-grid mid" x1="0" x2="${width}" y1="${y(series.max_tss / 2)}" y2="${y(series.max_tss / 2)}"></line>
        ${plannedArea}${targets}${recorded}${trajectory}
        ${series.rows.map((row) => `<rect class="season-week-hit" x="${x(row.left)}" y="0" width="${Math.max(0, x(row.right) - x(row.left))}" height="${baseline}"><title>${escapeHtml(`${dayLabel(row.start_date)}–${dayLabel(row.end_date)} · ${seasonWeekLoadLabel(row)}${row.target_note && row.target_min !== null ? ` · ${row.target_note}` : ""}`)}</title></rect>`).join("")}
      </svg>${hasData ? `<div class="season-chart-scale" aria-hidden="true"><span>${seasonTss(series.max_tss)} TSS</span><span>0 TSS</span></div>` : '<span class="season-chart-empty">No weekly TSS data</span>'}`;
    }

    function seasonHorizonLayout(phases, currentWeek, selectedDate, today, domain = null) {
      const dayMs = 86400000;
      const dateTime = (value) => {
        const text = String(value || "");
        if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(text)) return NaN;
        const valueMs = Date.parse(`${text}T00:00:00Z`);
        return Number.isFinite(valueMs) && new Date(valueMs).toISOString().slice(0, 10) === text
          ? valueMs : NaN;
      };
      const rows = (Array.isArray(phases) ? phases : [])
        .map((phase) => ({ phase, start: dateTime(phase?.start_date), end: dateTime(phase?.end_date) + dayMs }))
        .filter(({ start, end }) => Number.isFinite(start) && Number.isFinite(end) && end > start)
        .sort((left, right) => left.start - right.start);
      const domainStart = dateTime(domain?.start_date);
      const domainEnd = dateTime(domain?.end_date) + dayMs;
      const hasDomain = Number.isFinite(domainStart) && Number.isFinite(domainEnd) && domainEnd > domainStart;
      if (!hasDomain && !rows.length) return null;
      const spanStart = hasDomain ? domainStart : rows[0].start;
      const spanEnd = hasDomain ? domainEnd : Math.max(...rows.map((row) => row.end));
      const span = spanEnd - spanStart;
      const percent = (time) => Math.max(0, Math.min(100, ((time - spanStart) / span) * 100));
      const range = (start, end) => {
        const left = percent(start);
        return { left, width: Math.max(0, percent(end) - left) };
      };
      const rawWeekStart = dateTime(currentWeek?.start_date);
      const rawWeekEnd = dateTime(currentWeek?.end_date) + dayMs;
      const hasWeek = Number.isFinite(rawWeekStart) && Number.isFinite(rawWeekEnd) && rawWeekEnd > rawWeekStart;
      const weekStart = hasWeek ? rawWeekStart : spanStart;
      const weekEnd = hasWeek ? rawWeekEnd : spanStart;
      const rawMarker = dateTime(selectedDate);
      const marker = Number.isFinite(rawMarker) && weekStart <= rawMarker && rawMarker < weekEnd
        ? rawMarker : weekStart;
      const markerDate = new Date(marker).toISOString().slice(0, 10);
      const rawToday = dateTime(today);
      const todayMarker = Number.isFinite(rawToday) && spanStart <= rawToday && rawToday < spanEnd
        ? { left: percent(rawToday), date: today } : null;
      const months = [];
      const cursor = new Date(spanStart);
      cursor.setUTCDate(1);
      while (cursor.getTime() < spanEnd && months.length < 120) {
        const monthStart = cursor.getTime();
        const date = cursor.toISOString().slice(0, 10);
        cursor.setUTCMonth(cursor.getUTCMonth() + 1);
        months.push({ date, ...range(monthStart, cursor.getTime()) });
      }
      return {
        start_date: new Date(spanStart).toISOString().slice(0, 10),
        end_date: new Date(spanEnd - dayMs).toISOString().slice(0, 10),
        phases: rows.filter(({ start, end }) => end > spanStart && start < spanEnd).map(({ phase, start, end }) => ({
          name: String(phase.name || "Training block"),
          start_date: phase.start_date,
          end_date: phase.end_date,
          ...range(start, end)
        })),
        selection: range(weekStart, weekEnd),
        marker: hasWeek && spanStart <= marker && marker < spanEnd
          ? { left: percent(marker), date: markerDate, label: markerDate === today ? "Today" : "Selected day" } : null,
        today_marker: todayMarker,
        months
      };
    }

    function seasonRaceMarkers(events, layout) {
      const dayMs = 86400000;
      const start = Date.parse(`${layout.start_date}T00:00:00Z`);
      const end = Date.parse(`${layout.end_date}T00:00:00Z`) + dayMs;
      const dates = new Map();
      const seen = new Set();
      for (const event of Array.isArray(events) ? events : []) {
        const date = String(event?.date || "");
        if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(date) || eventIsSkipped(event)) continue;
        const time = Date.parse(`${date}T00:00:00Z`);
        if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== date || time < start || time >= end) continue;
        const name = String(event.name || "Unnamed event");
        const key = JSON.stringify([date, String(event.id || name), String(event.discipline || "")]);
        if (seen.has(key)) continue;
        seen.add(key);
        const status = String(event.status || "").toLowerCase();
        const tentative = event.markers?.maybe === true || ["maybe", "tentative"].includes(status)
          || !(event.markers?.commitment === true || ["confirmed", "committed"].includes(status));
        const detail = `${name}${event.discipline ? ` · ${event.discipline}` : ""}${event.priority ? ` · ${event.priority} priority` : ""}${tentative ? " · tentative" : ""}`;
        if (!dates.has(date)) dates.set(date, { date, left: 100 * (time - start) / (end - start), names: [], details: [], tentative: true });
        const group = dates.get(date);
        group.names.push(name);
        group.details.push(detail);
        group.tentative = group.tentative && tentative;
      }
      const occupied = [];
      return [...dates.values()].sort((left, right) => left.date.localeCompare(right.date)).map((group) => {
        let row = occupied.findIndex((last) => group.left - last >= 2.2);
        if (row < 0) row = occupied.length < 4 ? occupied.length : occupied.indexOf(Math.min(...occupied));
        occupied[row] = group.left;
        return { ...group, row, label: group.names.join(" / "), description: `${dayLabel(group.date)} · ${group.details.join("; ")}` };
      });
    }

    function renderSeasonHorizon(currentWeek, options = {}) {
      const full = options.scope === "calendar";
      const scope = full ? "calendar" : "current";
      const year = /^[0-9]{4}$/.test(String(options.year || "")) ? String(options.year) : TODAY.slice(0, 4);
      const domain = full ? { start_date: `${year}-01-01`, end_date: `${year}-12-31` } : null;
      const layout = seasonHorizonLayout(DATA.phases, currentWeek, state.selectedDate, TODAY, domain);
      if (!layout) return "";
      const loadSeries = seasonLoadSeries(DATA.weeks, layout, TODAY);
      const provenance = seasonLoadProvenance(loadSeries);
      const selectedLoad = loadSeries.rows.find((row) => row.start_date === currentWeek?.start_date);
      const loadSummary = seasonWeekLoadLabel(selectedLoad);
      const eventRows = Array.isArray(DATA.events) ? DATA.events
        : (DATA.days || []).flatMap((day) => (day.events || []).map((event) => ({ ...event, date: event.date || day.date })));
      const races = seasonRaceMarkers(eventRows, layout).map((event) => {
        const selectable = Boolean(dayByDate(event.date));
        return { ...event, selectable, description: `${event.description}${selectable ? "" : " · No loaded day for this event"}` };
      });
      const upcoming = races.filter((event) => event.date >= (currentWeek?.start_date || TODAY)).slice(0, 3);
      const arcLabel = full ? `${year} training load`
        : `${currentWeek?.phase || layout.phases[0]?.name || "Season"} → ${layout.phases.at(-1)?.name || "Plan"}`;
      const shownWeek = currentWeek ? `${dayLabel(currentWeek.start_date)}–${dayLabel(currentWeek.end_date)}` : "None selected";
      const selectedDescription = currentWeek
        ? `${shownWeek} · ${currentWeek.phase || "Training block"}${layout.selection.width > 0 ? "" : " · outside displayed season"}` : "No week selected";
      const selectedPhase = layout.selection.width > 0
        ? currentWeek?.phase || layout.phases.find((phase) => phase.start_date <= state.selectedDate && state.selectedDate <= phase.end_date)?.name || "" : "";
      const todayAnchor = todayAnchorDate();
      const canJumpToday = Boolean(dayByDate(todayAnchor));
      const todayDescription = todayAnchor === TODAY
        ? `Jump to today · ${dayLabel(TODAY)}`
        : `Today is ${dayLabel(TODAY)}; jump to the ${todayAnchor < TODAY ? "latest" : "next"} available day · ${dayLabel(todayAnchor)}`;
      const horizonWeeks = (DATA.weeks || []).filter((week) => week.end_date >= layout.start_date && week.start_date <= layout.end_date);
      const selectedIndex = Math.max(0, horizonWeeks.findIndex((week) => week.start_date === currentWeek?.start_date));
      const summaryId = `season-load-summary-${scope}`;
      const raceButton = (event, prefix) => `<button type="button" class="season-race" data-season-date="${escapeHtml(event.date)}" data-season-focus="${prefix}-${escapeHtml(event.date)}" title="${escapeHtml(event.description)}"${event.selectable ? "" : " disabled"}><span>${escapeHtml(dayLabel(event.date))}${event.tentative ? " · tentative" : ""}${event.selectable ? "" : " · no loaded day"}</span><strong>${escapeHtml(event.label)}</strong></button>`;
      return `
        <section class="season-horizon${full ? " season-overview-horizon" : ""}" aria-label="${full ? "Season training load" : "Season horizon"}" data-season-jump="${scope}" data-season-start="${escapeHtml(layout.start_date)}" data-season-end="${escapeHtml(layout.end_date)}">
          <div class="season-horizon-head">
            <div>
              <p class="eyebrow">Season plan</p>
              <strong>${escapeHtml(arcLabel)}</strong>
              ${full ? `<p class="season-overview-copy">${escapeHtml(provenance.note)} Boundary points retain their whole-week totals.</p>` : ""}
            </div>
            <div class="season-horizon-races">
              ${upcoming.map((event) => raceButton(event, "upcoming")).join("")}
            </div>
          </div>
          ${full ? `<p class="season-overview-stats"><span><strong>${provenance.budget}</strong> coach-budget weeks${provenance.provisional ? ` · ${provenance.provisional} provisional` : ""}</span><span><strong>${provenance.source}</strong> source-target weeks</span><span><strong>${provenance.prescribed}</strong> prescribed-session weeks</span><span><strong>${provenance.recorded}</strong> recorded weeks${provenance.incomplete ? ` · ${provenance.incomplete} incomplete` : ""}</span></p>` : ""}
          <div class="season-track-wrap">
            <div class="season-track-meta">
              <div class="season-chart-key"><strong>Weekly TSS</strong><span class="trajectory-key"><i aria-hidden="true"></i>Planned budget</span><span><i aria-hidden="true"></i>Intentional range</span><span class="recorded-key"><i aria-hidden="true"></i>Recorded load</span></div>
              <div class="season-track-actions">
                <span class="season-selection-key"><i aria-hidden="true"></i>Shown week · ${escapeHtml(shownWeek)}</span>
                <button type="button" class="season-today-button" data-season-today data-season-focus="today" aria-label="${escapeHtml(todayDescription)}" title="${escapeHtml(todayDescription)}"${canJumpToday ? "" : " disabled"}><i aria-hidden="true"></i>Today</button>
                ${full ? `<button type="button" class="season-open-week" data-season-open-week data-season-focus="open"${currentWeek ? "" : " disabled"}>Open week ↗</button>` : ""}
              </div>
            </div>
            <div class="season-track" ${horizonWeeks.length ? `data-season-track data-season-focus="track" role="slider" tabindex="0" aria-label="Select week from season horizon" aria-valuemin="0" aria-valuemax="${horizonWeeks.length - 1}" aria-valuenow="${selectedIndex}" aria-valuetext="${escapeHtml(selectedDescription)}"` : 'role="img" aria-label="Season TSS chart"'} aria-describedby="${summaryId}">
              ${renderSeasonLoadChart(loadSeries)}
              ${layout.phases.map((phase) => {
                const label = `${phase.name} · ${dayLabel(phase.start_date)}–${dayLabel(phase.end_date)}`;
                return `<div class="season-phase ${phaseTone(phase.name)}" data-season-phase aria-hidden="true" title="${escapeHtml(label)}" style="left:${phase.left}%; width:${phase.width}%"></div>`;
              }).join("")}
              ${layout.selection.width > 0 ? `<div class="season-selected-range" aria-hidden="true" style="left:${layout.selection.left}%; width:${layout.selection.width}%"></div>` : ""}
              ${layout.marker && layout.marker.date !== layout.today_marker?.date ? `<div class="season-day-marker" aria-hidden="true" title="${escapeHtml(layout.marker.label)}" style="left:${layout.marker.left}%"></div>` : ""}
              ${layout.today_marker ? `<div class="season-today-marker" aria-hidden="true" title="Today · ${escapeHtml(dayLabel(layout.today_marker.date))}" style="left:${layout.today_marker.left}%"></div>` : ""}
            </div>
            <div class="season-months">
              ${layout.months.map((month) => `<span style="left:${month.left}%; width:${month.width}%">${escapeHtml(utcDate(month.date).toLocaleString(undefined, { month: "short", timeZone: "UTC" }))}</span>`).join("")}
            </div>
            ${full && races.length ? `<div class="season-event-track" aria-label="Race and event dates" style="height:${23 + 18 * Math.max(...races.map((event) => event.row))}px">${races.map((event) => `<button type="button" class="season-event-marker${event.tentative ? " tentative" : ""}" data-season-race-marker data-season-date="${escapeHtml(event.date)}" data-season-focus="race-${escapeHtml(event.date)}" aria-label="${escapeHtml(event.description)}" title="${escapeHtml(event.description)}" style="left:${event.left}%; --event-row:${event.row}"${event.selectable ? "" : " disabled"}></button>`).join("")}</div>` : ""}
            <p class="season-load-readout" id="${summaryId}" aria-live="polite">${full && selectedPhase ? `<span data-season-phase-readout>Phase: ${escapeHtml(selectedPhase)} · </span>` : ""}${escapeHtml(loadSummary)}${horizonWeeks.length ? ` · Select a week for details${full ? "; Enter opens Week" : ""}` : ""}</p>
            ${full && races.length ? `<details class="season-event-list"><summary>${races.reduce((count, event) => count + event.names.length, 0)} races and events in ${escapeHtml(year)}</summary><div>${races.map((event) => raceButton(event, "event")).join("")}</div></details>` : ""}
          </div>
        </section>`;
    }

    function renderWeekSelect() {
      const select = document.getElementById("week-select");
      select.innerHTML = DATA.weeks.map((week) => {
        const label = `${dayLabel(week.start_date)} - ${dayLabel(week.end_date)} / ${truncate(week.phase || week.primary_focus || "Training", 28)}`;
        return `<option value="${escapeHtml(week.start_date)}">${escapeHtml(label)}</option>`;
      }).join("");
      select.value = state.selectedWeekStart || DATA.weeks[0]?.start_date || "";
      select.addEventListener("change", () => {
        state.selectedWeekStart = select.value;
        const week = DATA.weeks.find((item) => item.start_date === state.selectedWeekStart);
        if (week && !(week.start_date <= state.selectedDate && state.selectedDate <= week.end_date)) {
          state.selectedDate = week.start_date;
        }
        state.calendarYear = state.selectedDate.slice(0, 4);
        renderCalendarYearSelect();
        renderCalendar();
        renderWeek();
        renderCoachRail();
        renderTodayDashboard();
        renderMonthRail();
        renderRideSidebar();
        syncNavigationUrl();
      });
      document.getElementById("previous-week").addEventListener("click", () => moveWeek(-1));
      document.getElementById("next-week").addEventListener("click", () => moveWeek(1));
    }

    function selectedWeekIndex() {
      return DATA.weeks.findIndex((week) => week.start_date === state.selectedWeekStart);
    }

    function moveWeek(delta) {
      const current = selectedWeekIndex();
      const fallback = current >= 0 ? current : 0;
      const next = Math.max(0, Math.min(DATA.weeks.length - 1, fallback + delta));
      const week = DATA.weeks[next];
      if (!week) return;
      state.selectedWeekStart = week.start_date;
      state.selectedDate = week.start_date;
      state.calendarYear = state.selectedDate.slice(0, 4);
      renderCalendarYearSelect();
      renderCalendar();
      renderWeek();
      renderCoachRail();
      renderTodayDashboard();
      renderMonthRail();
      renderRideSidebar();
      syncNavigationUrl();
    }

    function updateWeekNavButtons() {
      const index = selectedWeekIndex();
      document.getElementById("previous-week").disabled = index <= 0;
      document.getElementById("next-week").disabled = index < 0 || index >= DATA.weeks.length - 1;
    }

    function nextUpcomingEvent() {
      return (Array.isArray(DATA.events) ? DATA.events : [])
        .filter((event) => event?.date && event.date >= TODAY && !eventIsSkipped(event))
        .sort((left, right) => left.date.localeCompare(right.date))[0] || null;
    }

    function renderWeekBudgetDetail(week) {
      const budget = week.coach_budget;
      if (!budget) return "";
      const needsReview = budget.state !== "current";
      const conditions = Array.isArray(budget.conditions) ? budget.conditions : [];
      const target = formatTssNumber(budget.target_tss);
      const ceiling = budget.ceiling_tss == null ? "" : ` · planning ceiling ${formatTssNumber(budget.ceiling_tss)} TSS`;
      return `<div class="week-budget-note" aria-label="Coach TSS budget rationale">
        <p><strong>${needsReview ? "Previous coach budget needs review" : "Coach budget"}</strong> · ${escapeHtml(budget.status || "provisional")} · ${escapeHtml(target)} TSS${escapeHtml(ceiling)}</p>
        <p>${escapeHtml(budget.rationale || "")}</p>
        ${needsReview ? '<p>The plan changed; this previous budget is not being used.</p>' : ""}
        ${conditions.length ? `<ul>${conditions.map((condition) => `<li>${escapeHtml(condition)}</li>`).join("")}</ul>` : ""}
      </div>`;
    }

    function renderWeek() {
      const weekRoot = document.getElementById("week-list");
      const horizonFocus = seasonFocusKey(weekRoot);
      const week = DATA.weeks.find((item) => item.start_date === state.selectedWeekStart) || DATA.weeks[0];
      if (!week) {
        const rider = String(DATA.athlete?.display_name || "Your").trim();
        const goal = DATA.goals?.primary_goal || DATA.goals?.north_star;
        const nextEvent = nextUpcomingEvent();
        const nextEventCopy = nextEvent
          ? `Next event: ${nextEvent.name || "Unnamed event"} · ${dayLabel(nextEvent.date)}${nextEvent.priority ? ` · ${nextEvent.priority} priority` : ""}`
          : "";
        const prompt = DATA.onboarding?.choices?.plan === "none"
          ? "Use $gradient-ascent to create a practical starter cycling week from my saved goals and availability."
          : "Use $gradient-ascent to continue my cycling setup and help me decide what to add next.";
        const activityAction = DATA.onboarding?.choices?.activities === "none"
          ? "Activity history skipped — add later"
          : "Add activity history";
        document.getElementById("week-list").innerHTML = `
          <article class="week-card" style="padding: 28px;">
            <p class="eyebrow">Setup complete</p>
            <h3 class="week-title">${escapeHtml(rider === "Your" ? "Your coaching workspace is ready" : `${rider}'s coaching workspace is ready`)}</h3>
            <p class="week-focus">${goal ? `Primary goal: ${escapeHtml(goal)}` : "Your profile is saved. No plan or dated rides are loaded yet."}</p>
            ${nextEventCopy ? `<p class="week-focus">${escapeHtml(nextEventCopy)}</p>` : ""}
            <div class="connection-actions empty-state-actions" style="margin-top: 18px;">
              <a class="connection-doc" href="${escapeHtml(codexThreadUrl(prompt))}">Discuss or create a plan in Codex</a>
              <button id="empty-open-connections" type="button">${escapeHtml(activityAction)}</button>
            </div>
          </article>`;
        document.getElementById("empty-open-connections")?.addEventListener("click", () => setView("connections"));
        return;
      }
      hydrateWeekActivityDetails(week.start_date);
      const select = document.getElementById("week-select");
      if (select.value !== week.start_date) select.value = week.start_date;
      state.selectedWeekStart = week.start_date;
      updateWeekNavButtons();
      const liveStatus = weekStatusForToday(week);
      const statusLabel = liveStatus.label;
      const statusCopy = weekStatusCopy(week, liveStatus.status);
      document.getElementById("week-list").innerHTML = `
        <article class="week-card">
          ${renderSeasonHorizon(week)}
          <div class="week-load-overview" aria-label="Scheduled and recorded week totals">
            <div><span>Scheduled hours</span><strong>${escapeHtml(week.planned_load?.hours_label || week.target_hours_label || "--")}</strong><small>${week.planned_load?.duration_source === "source_weekly_hours" ? "Weekly target" : "Scheduled duration"}</small></div>
            <div class="forecast-value" title="${escapeHtml(week.planned_load?.note || "")}"><span>TSS budget</span><strong>${escapeHtml(week.planned_load?.tss_value_label || "-- TSS")}</strong><small>${escapeHtml(week.planned_load?.qualifier || "Budget not set")}${week.planned_load?.budget_ceiling_label ? ` · ceiling ${escapeHtml(week.planned_load.budget_ceiling_label)}` : ""}${week.planned_load?.budget_review_required && week.planned_load?.estimated_tss != null ? " · previous coach budget needs review" : ""}${week.separate_structured_workout_count ? ` · ${week.separate_structured_workout_count} separate structured workout${week.separate_structured_workout_count === 1 ? "" : "s"}` : ""}</small></div>
            <div><span>Recorded hours</span><strong>${escapeHtml(Number(week.totals?.activity_count || 0) ? week.actual_hours_label : "--")}</strong><small>${Number(week.totals?.activity_count || 0) ? "Moving time" : "No recordings"}</small></div>
            <div title="${escapeHtml(week.tss_description || "")}"><span>Recorded TSS</span><strong>${escapeHtml(week.estimated_tss_label || "-- TSS")}</strong><small>${escapeHtml(week.tss_qualifier || "No supported load")}</small></div>
          </div>
          <div class="week-intel">
            <div class="week-thesis">
              <div class="week-desk-kicker">Week thesis</div>
              <h3 class="week-title">${escapeHtml(week.primary_focus || "Training week")}</h3>
              <p class="week-focus">${escapeHtml(week.why_logic || week.notes || "Keep the point of the week intact.")}</p>
            </div>
            <details class="week-status">
              <summary class="week-status-summary">
                <span class="week-stance-label">Week status</span>
                <span class="week-status-main">
                  <strong>${escapeHtml(statusLabel)}</strong>
                  <span class="week-status-disclosure" aria-hidden="true"></span>
                </span>
                <span class="week-status-copy">${escapeHtml(statusCopy)}</span>
                ${week.events?.length ? `
                  <span class="week-status-events">
                    <span class="week-stance-label">Week events</span>
                    <span class="event-row">${week.events.map((event) => renderEventChip(event, { compact: true })).join("")}</span>
                  </span>` : ""}
              </summary>
              <div class="week-status-details" aria-label="Week status details">
                <div class="week-status-metrics">
                  <div class="week-status-metric" title="${escapeHtml(week.tss_description || "")}">
                    <span>Recorded TSS</span>
                    <strong>${escapeHtml(week.estimated_tss_label || "-- TSS")}</strong>
                    ${week.tss_qualifier ? `<small class="load-qualifier">${escapeHtml(week.tss_qualifier)}</small>` : ""}
                  </div>
                  <div class="week-status-metric" title="${escapeHtml(week.planned_load?.note || "")}">
                    <span>TSS budget</span>
                    <strong>${escapeHtml(week.planned_load?.tss_value_label || "-- TSS")}</strong>
                    <small class="load-qualifier">${escapeHtml(week.planned_load?.qualifier || "Not specified")}</small>
                  </div>
                  <div class="week-status-metric">
                    <span>Meaningful hours</span>
                    <strong>${escapeHtml(week.meaningful_ride_hours_label || "0.0h")}</strong>
                  </div>
                  <div class="week-status-metric">
                    <span>Target hours</span>
                    <strong>${escapeHtml(week.target_hours_label || "--")}</strong>
                  </div>
                  <div class="week-status-metric">
                    <span>Recorded hours</span>
                    <strong>${escapeHtml(week.actual_hours_label || "0.0h")}</strong>
                  </div>
                  <div class="week-status-metric">
                    <span>Energy</span>
                    <strong>${escapeHtml(week.kilojoules_label || "0 kJ")}</strong>
                  </div>
                  <div class="week-status-metric">
                    <span>Distance</span>
                    <strong>${escapeHtml(weekDistanceLabel(week))}</strong>
                  </div>
                  <div class="week-status-metric">
                    <span>Rides</span>
                    <strong>${escapeHtml(String(week.totals?.activity_count || 0))}</strong>
                  </div>
                </div>
                ${renderWeekBudgetDetail(week)}
              </div>
            </details>
            <aside class="week-stance" aria-label="Week coaching read">
              <div class="week-stance-copy">
                <span class="week-stance-label">Coach read</span>
                <strong>${escapeHtml(week.phase || "Keep the shape.")}</strong>
                <p>${escapeHtml(week.execution_note || week.notes || "The work only counts if it still serves the thing you are actually trying to become.")}</p>
              </div>
            </aside>
          </div>
          <div class="week-days">
            ${week.days.map(renderWeekDay).join("")}
          </div>
        </article>`;
      bindWeekCards();
      bindSeasonHorizon();
      restoreSeasonFocus(weekRoot, horizonFocus);
      requestAnimationFrame(syncWeekIntervalLists);
    }

    function bindSeasonHorizon(horizon = document.querySelector("#week-list [data-season-jump='current']")) {
      if (!horizon) return;
      const scope = horizon.dataset.seasonJump === "calendar" ? "calendar" : "current";
      const currentHorizon = () => document.querySelector(`[data-season-jump="${scope}"]`);
      const focus = (key) => restoreSeasonFocus(currentHorizon(), key);
      const openWeek = () => {
        if (!DATA.weeks.some((week) => week.start_date === state.selectedWeekStart)) return;
        renderWeek();
        setView("weeks");
        restoreSeasonFocus(document.getElementById("week-list"), "track");
      };
      horizon.querySelector("[data-season-today]")?.addEventListener("click", () => {
        refreshCurrentDate();
        const anchor = todayAnchorDate();
        if (!dayByDate(anchor)) return;
        selectDate(anchor, { switchWeek: true });
        focus("today");
      });
      horizon.querySelector("[data-season-open-week]")?.addEventListener("click", openWeek);
      horizon.querySelectorAll("[data-season-date]").forEach((button) => {
        button.addEventListener("click", () => {
          selectDate(button.dataset.seasonDate, { switchWeek: true, openRide: scope === "current" });
          focus(button.dataset.seasonFocus);
        });
      });
      const track = horizon.querySelector("[data-season-track]");
      if (!track) return;
      const horizonWeeks = DATA.weeks.filter((week) =>
        week.end_date >= horizon.dataset.seasonStart && week.start_date <= horizon.dataset.seasonEnd
      );
      const weeks = horizonWeeks;
      const showWeek = (week, target = week?.start_date, restoreFocus = false) => {
        if (!week) return;
        const first = week.start_date > horizon.dataset.seasonStart ? week.start_date : horizon.dataset.seasonStart;
        const last = week.end_date < horizon.dataset.seasonEnd ? week.end_date : horizon.dataset.seasonEnd;
        const candidate = target >= first && target <= last ? target : first;
        const selected = dayByDate(candidate) ? candidate
          : (DATA.days || []).find((day) => first <= day.date && day.date <= last)?.date;
        if (!selected) return;
        state.selectedWeekStart = week.start_date;
        selectDate(selected, { switchWeek: false });
        if (restoreFocus) focus("track");
      };
      track.addEventListener("click", (event) => {
        const rect = track.getBoundingClientRect();
        if (rect.width <= 0) return;
        const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        const start = utcDate(horizon.dataset.seasonStart).getTime();
        const end = utcDate(horizon.dataset.seasonEnd).getTime() + 86400000;
        const target = new Date(Math.min(end - 1, start + Math.max(1, end - start) * ratio)).toISOString().slice(0, 10);
        const week = weeks.find((item) => item.start_date <= target && target <= item.end_date) ||
          weeks.reduce((nearest, item) => {
            const center = (utcDate(item.start_date).getTime() + utcDate(item.end_date).getTime()) / 2;
            const nearestCenter = nearest
              ? (utcDate(nearest.start_date).getTime() + utcDate(nearest.end_date).getTime()) / 2
              : Number.POSITIVE_INFINITY;
            return Math.abs(center - utcDate(target).getTime()) < Math.abs(nearestCenter - utcDate(target).getTime())
              ? item
              : nearest;
          }, null);
        showWeek(week, target);
      });
      track.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && scope === "calendar") {
          event.preventDefault();
          openWeek();
          return;
        }
        let index = Math.max(0, weeks.findIndex((week) => week.start_date === state.selectedWeekStart));
        if (event.key === "ArrowLeft" || event.key === "ArrowDown") index -= 1;
        else if (event.key === "ArrowRight" || event.key === "ArrowUp") index += 1;
        else if (event.key === "Home") index = 0;
        else if (event.key === "End") index = weeks.length - 1;
        else return;
        event.preventDefault();
        showWeek(weeks[Math.max(0, Math.min(weeks.length - 1, index))], undefined, true);
      });
    }

    function renderEventChip(event, options = {}) {
      const date = options.compact ? "" : `${event.date || ""} `;
      return `<span class="event-chip">${escapeHtml(date)}${escapeHtml(event.name)} ${event.discipline ? `/${escapeHtml(event.discipline)}` : ""}</span>`;
    }

    function summaryCard(value, label, options = {}) {
      return `
        <div class="summary-card"${options.description ? ` title="${escapeHtml(options.description)}"` : ""}>
          <div class="summary-value">${escapeHtml(value)}</div>
          <div class="stat-label">${escapeHtml(label)}</div>
          ${options.qualifier ? `<small class="load-qualifier">${escapeHtml(options.qualifier)}</small>` : ""}
        </div>`;
    }

    function primaryActivity(day) {
      const activities = day.activities || [];
      return activities.find((activity) => activity.meaningful) || activities[0] || null;
    }

    function compactStat(value, label, className = "", options = {}) {
      return `
        <div class="${className || "week-stat-chip"}"${options.description ? ` title="${escapeHtml(options.description)}"` : ""}>
          <strong>${escapeHtml(value || "--")}</strong>
          <span>${escapeHtml(label)}</span>
          ${options.qualifier ? `<small class="load-qualifier">${escapeHtml(options.qualifier)}</small>` : ""}
        </div>`;
    }

    function dayTssLabel(day, includeUnit = true) {
      const metrics = day?.metrics || {};
      const supplied = includeUnit ? metrics.tss_label : metrics.tss_short_label;
      if (supplied != null) return supplied;
      const value = metrics.estimated_tss;
      if (value == null || !Number.isFinite(Number(value)) || Number(value) < 0) return includeUnit ? "-- TSS" : "--";
      return `${formatTssNumber(value)}${includeUnit ? " TSS" : ""}`;
    }

    function dayLoadOptions(day) {
      const metrics = day?.metrics || {};
      return {
        description: metrics.tss_description || "",
        qualifier: metrics.tss_qualifier || (metrics.tss_estimated ? "Calculated" : "")
      };
    }

    function weekDayLoadWarning(day) {
      return dayLoadOptions(day).qualifier.split(" · ")
        .map((part) => part.trim())
        .filter((part) => part && part !== "Calculated" && part !== "Source")
        .join(" · ");
    }

    function durationLabelFromHours(hoursValue) {
      const totalMinutes = Math.round(Number(hoursValue || 0) * 60);
      if (!Number.isFinite(totalMinutes) || totalMinutes <= 0) return "0min";
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      if (!hours) return `${minutes}min`;
      return minutes ? `${hours}hr ${minutes}min` : `${hours}hr`;
    }

    function dayTimeLabel(day) {
      const metrics = day.metrics || {};
      const hours = Number(metrics.meaningful_ride_hours || metrics.moving_hours || 0);
      return durationLabelFromHours(hours);
    }

    function plannedDayTimeLabel(day) {
      if (day?.planned_load?.hours_label != null) return day.planned_load.hours_label;
      const hours = day?.planned_load?.hours;
      return hours == null ? "--" : durationLabelFromHours(hours);
    }

    function plannedDayTssLabel(day) {
      if (day?.planned_load?.tss_value_label != null) return day.planned_load.tss_value_label;
      const value = day?.planned_load?.estimated_tss;
      if (value == null) return "--";
      const tss = Number(value);
      return `${formatTssNumber(tss)} TSS`;
    }

    function numericLabel(value, suffix, digits = 0, prefix = "") {
      const number = Number(value);
      if (!Number.isFinite(number) || number <= 0) return null;
      return `${prefix}${formatNumber(number, digits)}${suffix}`;
    }

    function distanceLabelFromKm(kmValue) {
      const km = Number(kmValue || 0);
      if (!Number.isFinite(km) || km <= 0) return UNIT_SYSTEM === "metric" ? "0.0 km" : "0.0 mi";
      return UNIT_SYSTEM === "metric"
        ? `${formatNumber(km, 1)} km`
        : `${formatNumber(km * 0.621371, 1)} mi`;
    }

    function dayMetricLine(day) {
      const metrics = day?.metrics || {};
      if (!Number(metrics.activity_count || 0)) return "No synced ride yet.";
      const parts = [
        `${formatNumber(Number(metrics.moving_hours || 0), 1)}h moving`,
        distanceLabelFromKm(metrics.distance_km),
        `${formatNumber(Number(metrics.kilojoules || 0), 0)} kJ`
      ];
      if (metrics.estimated_tss != null) parts.push(`${dayTssLabel(day)}${metrics.tss_qualifier ? ` (${metrics.tss_qualifier})` : ""}`);
      if (metrics.average_heartrate != null) parts.push(`HR ${formatNumber(Number(metrics.average_heartrate), 0)}`);
      return parts.join(" | ");
    }

    function distanceUnitLabel() {
      return UNIT_SYSTEM === "metric" ? "kilometers" : "miles";
    }

    function dayDistanceLabel(day) {
      const km = Number(day.metrics?.distance_km || 0);
      return distanceLabelFromKm(km);
    }

    function weekDistanceLabel(week) {
      return distanceLabelFromKm(Number(week?.totals?.distance_m || 0) / 1000);
    }

    function activityDistanceLabel(activity) {
      return distanceLabelFromKm(activity?.distance_km);
    }

    function activityElevationLabel(activity) {
      const meters = Number(activity?.elevation_m);
      if (!Number.isFinite(meters)) return "-- climb";
      return UNIT_SYSTEM === "metric"
        ? `${formatNumber(meters, 0)} m climb`
        : `${formatNumber(meters * 3.28084, 0)} ft climb`;
    }

    function dayLapCount(day) {
      return (day.activities || []).reduce((sum, activity) => sum + Number(activity.lap_count || 0), 0);
    }

    function dayLapLabel(day) {
      const laps = dayLapCount(day);
      return laps ? `${laps} lap${laps === 1 ? "" : "s"}` : "0 laps";
    }

    function rideChipItems(day) {
      const metrics = day.metrics || {};
      const primary = primaryActivity(day);
      return [
        [dayTimeLabel(day), "time"],
        [dayTssLabel(day), "TSS", dayLoadOptions(day)],
        [primary?.np_label || "-- NP", "NP", { description: primary?.np_description }],
        [dayDistanceLabel(day), distanceUnitLabel()],
        [numericLabel(metrics.kilojoules, " kJ", 0) || "0 kJ", "kJ"],
        [numericLabel(metrics.average_heartrate || primary?.avg_hr, "", 0, "HR ") || "HR --", "avg HR"],
      ];
    }

    function renderIntervalList(activity, options = {}) {
      const labels = (activity?.interval_labels || []).filter(Boolean);
      const emptyLabel = options.emptyLabel || "";
      if (!labels.length) {
        return emptyLabel ? `<div class="interval-list"><span class="interval-chip quiet">${escapeHtml(emptyLabel)}</span></div>` : "";
      }
      const limit = Number(options.limit || labels.length);
      const overflowLabel = labels.length > limit ? options.overflowLabel || "" : "";
      return `
        <div class="interval-list">
          ${labels.slice(0, limit).map((label) => `<span class="interval-chip">${escapeHtml(label)}</span>`).join("")}
          ${overflowLabel ? `<span class="interval-chip quiet">${escapeHtml(overflowLabel)}</span>` : ""}
        </div>`;
    }

    function renderWeekRideMeta(day) {
      if (!day.has_synced_ride) {
        return "";
      }
      const labels = (primaryActivity(day)?.interval_labels || []).filter(Boolean);
      if (!labels.length) {
        return '<div class="interval-list week-interval-list"><span class="interval-chip quiet">Intervals --</span></div>';
      }
      const [firstLabel, ...extraLabels] = labels;
      const popoverId = `week-interval-popover-${day.date}`;
      return `
        <div class="interval-list week-interval-list" data-extra-count="${extraLabels.length}">
          <span class="interval-chip interval-primary">${escapeHtml(firstLabel)}</span>
          ${extraLabels.map((label) => `<span class="interval-chip interval-extra">${escapeHtml(label)}</span>`).join("")}
          ${extraLabels.length
            ? `<button class="interval-chip quiet interval-more" type="button" popovertarget="${escapeHtml(popoverId)}" aria-label="Show ${extraLabels.length} more interval labels" hidden>+${extraLabels.length}</button>`
            : ""}
        </div>
        ${extraLabels.length
          ? `
            <div class="interval-popover" id="${escapeHtml(popoverId)}" popover>
              <p class="section-title">More interval labels</p>
              <div class="interval-popover-list">
                ${extraLabels.map((label) => `<span class="interval-chip">${escapeHtml(label)}</span>`).join("")}
              </div>
            </div>`
          : ""}`;
    }

    function renderWeekRideFooter(day) {
      const cue = weekDayCue(day);
      const plan = day.planned_load || {};
      const loadOptions = dayLoadOptions(day);
      const loadWarning = weekDayLoadWarning(day);
      return `
        <div class="week-day-footer" aria-label="${day.has_synced_ride ? "Recorded" : "Scheduled"} day stats">
          ${day.has_synced_ride ? `
            <span class="week-stats-caption">Recorded</span>
            <div class="week-stat-chip-grid">
              ${compactStat(dayTssLabel(day), "TSS", "", { description: loadOptions.description })}
              ${compactStat(dayTimeLabel(day), "time")}
            </div>
            ${loadWarning ? `<p class="week-load-note" title="${escapeHtml(loadOptions.description)}">${escapeHtml(loadWarning)}</p>` : ""}` : `
            <div class="week-plan-summary" title="${escapeHtml(plan.note || "")}">
              <span class="week-stats-caption">Scheduled</span>
              <div class="week-plan-values"><strong>${escapeHtml(plannedDayTssLabel(day))}</strong><strong>${escapeHtml(plannedDayTimeLabel(day))}</strong></div>
              ${plan.estimated ? `<p class="week-load-note">${escapeHtml(plan.qualifier || "Forecast")}</p>` : ""}
            </div>`}
          ${cue ? `<p class="ride-cue">${escapeHtml(cue)}</p>` : ""}
        </div>`;
    }

    function renderIndependentWorkouts(day) {
      if (day.structured_is_primary || !day.structured_workouts?.length) return "";
      return `<div class="week-structured-plan"><span class="week-stats-caption">Separate structured plan</span>${day.structured_workouts.map((workout) => `<div title="${escapeHtml(workout.load?.note || "")}">${escapeHtml(workout.name)} · ${escapeHtml(workout.load?.hours_label || "--")}${workout.load?.estimated_tss != null ? ` · ${escapeHtml(workout.load.tss_value_label)}` : ""}</div>`).join("")}</div>`;
    }

    function weekDayCue(day) {
      const text = String(day?.planned || "").toLowerCase();
      if (/rest|off|recovery/.test(text)) return "Absorb, don't add. Keep it easy.";
      if (/vo2|anaerobic|sprint/.test(text)) return "Touch of speed. Stay controlled.";
      if (/threshold|sweet spot|ss\b|over.?under/.test(text)) return "Make it count. Leave one in reserve.";
      if (/z2|zone 2|endurance/.test(text)) return "Stay aerobic. Keep it honest.";
      if (/race|hopper|crit/.test(text)) return "Race it. Enjoy it. Recover tomorrow.";
      return "";
    }

    function renderWeekDay(day) {
      const signals = calendarDaySignals(day);
      const classes = [
        "week-day",
        day.date === state.selectedDate ? "selected" : "",
        hasDailyNote(day) ? "has-note" : "",
        day.has_synced_ride ? "has-ride" : "",
        day.hard_activity || day.planned_hard_day ? "hard-day" : ""
      ]
        .filter(Boolean).join(" ");
      return `
        <article class="${classes}" data-date="${escapeHtml(day.date)}" data-jump-date="${escapeHtml(day.date)}" role="button" tabindex="0">
          <div class="week-day-head">
            <span class="date-label">${escapeHtml(day.weekday)} ${escapeHtml(dayLabel(day.date))}</span>
          </div>
          ${renderDaySpark(day)}
          ${renderWeekDaySignals(day, signals)}
          <div class="week-day-title-stack">
            ${day.has_synced_ride
              ? `
                <p class="actual"${day.actual_title_from_plan ? ' title="Workout name from the plan; recorded stats are shown below"' : ""}>${escapeHtml(day.actual || "Synced ride")}</p>
                ${renderWeekStravaLink(day)}
                <p class="planned">${escapeHtml(day.actual_title_from_plan ? "Workout name from plan" : day.planned || "No planned session")}</p>`
              : `<p class="actual">${escapeHtml(day.planned || "No planned session")}</p>`}
          </div>
          <div class="week-day-meta">
            ${day.events?.length ? `<p class="event-row">${day.events.map((event) => renderEventChip(event, { compact: true })).join("")}</p>` : ""}
            ${renderWeekRideMeta(day)}
            ${renderIndependentWorkouts(day)}
          </div>
          ${renderWeekRideFooter(day)}
        </article>`;
    }

    function bindWeekCards() {
      document.querySelectorAll(".week-day[data-jump-date]").forEach((card) => {
        card.addEventListener("click", (event) => {
          if (event.target.closest("textarea, a, button, select")) return;
          selectDate(card.dataset.jumpDate, { openRide: true });
        });
        card.addEventListener("keydown", (event) => {
          if (event.target.closest("textarea, a, button, select")) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectDate(card.dataset.jumpDate, { openRide: true });
          }
        });
      });
      document.querySelectorAll(".week-day-signals").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          const expanded = button.classList.toggle("expanded");
          button.setAttribute("aria-expanded", String(expanded));
          button.setAttribute("aria-label", `${expanded ? "Hide" : "Show"} day signals`);
          button.querySelectorAll(".week-day-signal").forEach((signal) => {
            signal.textContent = expanded ? signal.dataset.full : signal.dataset.short;
          });
        });
      });
    }

    function syncWeekIntervalLists() {
      document.querySelectorAll(".week-interval-list").forEach((list) => {
        const more = list.querySelector(".interval-more");
        const extraCount = Number(list.dataset.extraCount || 0);
        if (!more || !extraCount) return;
        list.classList.remove("compact");
        more.hidden = true;
        const shouldCompact = list.scrollWidth > list.clientWidth + 1;
        list.classList.toggle("compact", shouldCompact);
        more.hidden = !shouldCompact;
      });
    }

    function statList(items) {
      const cleanItems = items.filter(([label, value]) => value !== null && value !== undefined && value !== "");
      if (!cleanItems.length) return "";
      return `
        <div class="stat-list">
          ${cleanItems.map(([label, value, description]) => `
            <div class="stat-row"${description ? ` title="${escapeHtml(description)}"` : ""}>
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value || "--")}</strong>
            </div>`).join("")}
        </div>`;
    }

    function statOrDash(value, suffix = "") {
      if (value === null || value === undefined || value === "") return "--";
      const number = Number(value);
      if (!Number.isFinite(number)) return String(value);
      return `${formatNumber(number, Number.isInteger(number) ? 0 : 1)}${suffix}`;
    }

    function rideAssessmentStats(day) {
      if (!day.has_synced_ride) return "";
      return `
        <div class="ride-assessment-stats">
          <div class="ride-stat-grid">
            ${rideChipItems(day).map(([value, label, options]) => compactStat(value, label, "ride-stat-chip", options)).join("")}
          </div>
        </div>`;
    }

    function renderActualRideLink(day) {
      const text = day.actual || "No synced ride yet.";
      const activity = primaryActivity(day);
      const url = activity?.source_url || activity?.strava_url || "";
      if (!url || !day.has_synced_ride) return escapeHtml(text);
      return `<a class="actual-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(text)}</a>`;
    }

    function renderWeekStravaLink(day) {
      const activity = primaryActivity(day);
      const url = activity?.source_url || activity?.strava_url || "";
      if (!url || !day.has_synced_ride) return "";
      return `<p class="week-day-strava"><a class="actual-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(activity?.source_label || "Strava")}</a></p>`;
    }

    function renderPlanExecution(day) {
      return `
        <div class="plan-execution-block">
          <p class="sidebar-copy"><strong>Plan:</strong> ${escapeHtml(day.planned || "No planned session")}</p>
          <p class="sidebar-copy"><strong>Actual:</strong> ${renderActualRideLink(day)}</p>
          ${day.events?.length ? `<p class="event-row">${day.events.map(renderEventChip).join("")}</p>` : ""}
        </div>`;
    }

    function renderSidebarStats(day) {
      const metrics = day.metrics || {};
      const primary = primaryActivity(day);
      if (!primary) {
        return '<p class="sidebar-copy">No synced ride analysis for this day yet.</p>';
      }
      return `
        <p class="stat-section-title">Ride analysis</p>
        ${statList([
          ["intensity", primary?.if_label || "IF --"],
          ["variability", primary?.vi_label || "VI --"],
          ["elevation", activityElevationLabel(primary)],
          ["Strava suffer", primary?.suffer_score_label || "-- suffer"],
          ["laps", primary?.lap_count_label || dayLapLabel(day)],
          ["activity count", statOrDash(metrics.activity_count)],
        ])}`;
    }

    function renderRecoveryStats(day) {
      const recovery = day.recovery || {};
      const status = recovery.status_label || "Recovery data unavailable";
      return `
        <p class="sidebar-copy">${escapeHtml(status)}</p>
        ${statList([
          ["resting HR", recovery.resting_hr_label || "--"],
          ["HRV", recovery.hrv_label || "--"],
          ["sleep", recovery.sleep_label || "--"],
          ["sleep score", recovery.sleep_score_label || "--"],
          ["readiness", recovery.readiness_label || "--"],
          ["stress", recovery.stress_label || "--"],
        ])}`;
    }

    function renderLapList(activity) {
      const laps = activity?.laps || [];
      if (!laps.length) return "";
      return `
        <div class="lap-list" aria-label="Lap stats">
          <div class="lap-row header">
            <span>Lap</span><span>Time</span><span>Avg HR</span><span>NP</span><span>Avg W</span>
          </div>
          ${laps.map((lap) => `
            <div class="lap-row">
              <strong>${escapeHtml(lap.label || "Lap")}</strong>
              <strong>${escapeHtml(lap.duration_label || "--")}</strong>
              <strong>${escapeHtml(lap.hr_label || "HR --")}</strong>
              <strong>${escapeHtml(lap.np_label || "-- NP")}</strong>
              <strong>${escapeHtml(lap.avg_watts_label || "-- W")}</strong>
            </div>`).join("")}
        </div>`;
    }

    function renderActivityCard(activity) {
      const notes = [
        activity.start_label,
        activity.sport,
        activity.meaningful ? "meaningful" : activity.exclusion_reason || "",
        activity.sport || ""
      ].filter(Boolean).join(" / ");
      const labels = (activity.labels || []).map((item) => item.label).filter(Boolean);
      const reaction = activity.reaction || "";
      const activityUrl = activity.source_url || activity.strava_url;
      const activityTitle = activityUrl
        ? `<a href="${escapeHtml(activityUrl)}" target="_blank" rel="noreferrer">${escapeHtml(activity.name || "Activity")}</a>`
        : `<strong>${escapeHtml(activity.name || "Activity")}</strong>`;
      return `
        <article class="activity-card">
          ${activityTitle}
          ${(labels.length || reaction) ? `
            <div class="day-tags">
              ${labels.map((label) => `<span class="day-tag custom">${escapeHtml(label)}</span>`).join("")}
              ${reaction ? `<span class="day-tag reaction" title="Coach reaction">${escapeHtml(reaction)}</span>` : ""}
            </div>` : ""}
          ${statList([
            ["time", activity.duration_label],
            [distanceUnitLabel(), activityDistanceLabel(activity)],
            [activity.tss_estimated ? "Calculated TSS" : "TSS", activity.tss_label, activity.tss_description],
            ["kilojoules", activity.kilojoules_label],
            ["NP", activity.np_label, activity.np_description],
            ["avg HR", activity.hr_label],
            ["intensity", activity.if_label],
            ["variability", activity.vi_label],
            ["suffer", activity.suffer_score_label],
            ["elevation", activityElevationLabel(activity)],
          ])}
          ${activity.tss_estimated || activity.tss_partial ? `<p class="sidebar-copy">${escapeHtml(activity.tss_description)}</p>` : ""}
          ${renderLapList(activity)}
          ${notes ? `<p class="sidebar-copy">${escapeHtml(notes)}</p>` : ""}
        </article>`;
    }

    function renderCoachNotes(day) {
      const notes = coachNotesForDay(day);
      if (!notes.length) {
        return '<p class="sidebar-copy">No coach notes yet.</p>';
      }
      return `
        <div class="coach-note-list">
          ${notes.map((note) => {
            const meta = [
              note.created_at ? String(note.created_at).slice(0, 10) : "",
              note.activity_name || "",
              ...(Array.isArray(note.tags) ? note.tags : [])
            ].filter(Boolean);
            return `
              <article class="coach-note-card">
                <div class="coach-note-card-head">
                  <strong>${escapeHtml(note.title || "Coach note")}</strong>
                  ${note.codex_url ? `<a href="${escapeHtml(note.codex_url)}">Codex</a>` : ""}
                </div>
                <p class="sidebar-copy">${escapeHtml(note.note || "")}</p>
                ${meta.length ? `<p class="activity-meta">${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</p>` : ""}
              </article>`;
          }).join("")}
        </div>`;
    }

    function renderRideSidebarContent(day) {
      const signals = calendarDaySignals(day);
      const summary = day.has_synced_ride
        ? `<p class="sidebar-copy">${renderActualRideLink(day)}</p>${rideAssessmentStats(day)}`
        : renderPlanExecution(day);
      const activityCards = (day.activities || []).length
        ? day.activities.map(renderActivityCard).join("")
        : '<p class="sidebar-copy">No synced Strava ride for this day.</p>';
      return `
        <section class="sidebar-section">
          ${renderDayTags(day, signals)}
          <h3>${day.has_synced_ride ? "Ride summary" : "Plan detail"}</h3>
          ${summary}
        </section>
        <section class="sidebar-section">
          <h4>Coach notes</h4>
          ${renderCoachNotes(day)}
        </section>
        <section class="sidebar-section">
          <h4>Analysis</h4>
          ${renderSidebarStats(day)}
        </section>
        <section class="sidebar-section">
          <h4>Night + recovery</h4>
          ${renderRecoveryStats(day)}
        </section>
        <section class="sidebar-section">
          <h4>Strava</h4>
          <div class="activity-list">${activityCards}</div>
        </section>
        `;
    }

    function renderRideSidebar() {
      const sidebar = document.getElementById("ride-sidebar");
      if (!sidebar) return;
      const day = dayByDate(state.selectedDate);
      if (!state.rideSidebarOpen || !day) {
        sidebar.classList.remove("open");
        sidebar.setAttribute("aria-hidden", "true");
        return;
      }
      sidebar.classList.add("open");
      sidebar.setAttribute("aria-hidden", "false");
      document.getElementById("ride-sidebar-content").innerHTML = renderRideSidebarContent(day);
    }

    function selectDate(value, options = {}) {
      if (!dayByDate(value)) return;
      state.selectedDate = value;
      state.calendarYear = state.selectedDate.slice(0, 4);
      renderCalendarYearSelect();
      const week = weekForDate(value);
      if (options.switchWeek && week) {
        state.selectedWeekStart = week.start_date;
      }
      renderCalendar();
      renderWeek();
      renderCoachRail();
      renderTodayDashboard();
      renderMonthRail();
      if (options.openRide || state.rideSidebarOpen) {
        state.rideSidebarOpen = true;
        renderRideSidebar();
      }
      if (options.scroll) {
        requestAnimationFrame(() => scrollToDate(value));
      }
      syncNavigationUrl();
    }

    function scrollToDate(value) {
      const target = document.querySelector(`.calendar-day[data-date="${value}"]`);
      if (!target) return;
      const rect = target.getBoundingClientRect();
      const top = window.scrollY + rect.top - Math.max(80, (window.innerHeight - rect.height) / 2);
      window.scrollTo({ top: Math.max(0, top), behavior: "auto" });
      target.focus({ preventScroll: true });
    }

    function allDayRows() {
      return DATA.days.map((day) => ({
        date: day.date,
        weekday: day.weekday,
        week_start: day.week_start,
        week_focus: day.week_focus || "",
        phase: day.phase || "",
        planned: day.planned || "",
        planned_tss: day.planned_load?.estimated_tss ?? "",
        planned_tss_min: day.planned_load?.estimated_tss_min ?? "",
        planned_tss_max: day.planned_load?.estimated_tss_max ?? "",
        planned_hours: day.planned_load?.hours ?? "",
        planned_intensity: day.planned_load?.intensity || "",
        planned_load_confidence: day.planned_load?.confidence || "",
        actual: day.actual || "",
        events: (day.events || []).map((event) => event.name).join("; "),
        moving_hours: day.metrics?.moving_hours ?? "",
        meaningful_ride_hours: day.metrics?.meaningful_ride_hours ?? "",
        distance_km: day.metrics?.distance_km ?? "",
        distance_miles: day.metrics?.distance_km ? Number((day.metrics.distance_km * 0.621371).toFixed(1)) : "",
        kilojoules: day.metrics?.kilojoules ?? "",
        estimated_tss: day.metrics?.estimated_tss ?? "",
        average_heartrate: day.metrics?.average_heartrate ?? "",
        lap_count: dayLapCount(day) || "",
        primary_activity: primaryActivity(day)?.name || "",
        coach_notes: coachNotesForDay(day).map((note) => note.note || "").filter(Boolean).join("\\n\\n"),
        coach_note_links: coachNotesForDay(day).map((note) => note.codex_url || "").filter(Boolean).join("\\n"),
        normalized_power: primaryActivity(day)?.np_watts ?? "",
        resting_hr: day.recovery?.resting_hr ?? "",
        hrv_ms: day.recovery?.hrv_ms ?? "",
        sleep_duration_s: day.recovery?.sleep_duration_s ?? "",
        sleep_score: day.recovery?.sleep_score ?? "",
        readiness_score: day.recovery?.readiness_score ?? "",
        stress_avg: day.recovery?.stress_avg ?? "",
        daily_note: noteForDay(day)
      }));
    }

    function csvEscape(value) {
      const text = String(value ?? "");
      return /[",\\n\\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    }

    function toCsv(rows) {
      if (!rows.length) return "";
      const headers = Object.keys(rows[0]);
      return [
        headers.map(csvEscape).join(","),
        ...rows.map((row) => headers.map((header) => csvEscape(row[header])).join(","))
      ].join("\\n") + "\\n";
    }

    function downloadText(filename, mime, text) {
      const blob = new Blob([text], { type: mime });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    }

    function exportWorkbook(rows, filename, csvFallbackName) {
      if (!window.XLSX) {
        downloadText(csvFallbackName, "text/csv;charset=utf-8", toCsv(rows));
        setStatus(`Exported ${csvFallbackName}`);
        return;
      }
      const sheet = XLSX.utils.json_to_sheet(rows);
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, sheet, "Training Center");
      XLSX.writeFile(workbook, filename);
      setStatus(`Exported ${filename}`);
    }

    function bindImportExport() {
      document.getElementById("export-all-xlsx").addEventListener("click", () => {
        exportWorkbook(allDayRows(), "training-center-day-rows.xlsx", "training-center-day-rows.csv");
        closeActionMenu();
      });
      const dialog = document.getElementById("plan-export-dialog");
      const start = document.getElementById("plan-export-start");
      const end = document.getElementById("plan-export-end");
      const status = document.getElementById("plan-export-status");
      const download = document.getElementById("plan-export-download");
      document.getElementById("export-planned-schedule").addEventListener("click", () => {
        const through = new Date(`${TODAY}T12:00:00Z`);
        through.setUTCDate(through.getUTCDate() + 41);
        start.value = TODAY;
        end.value = through.toISOString().slice(0, 10);
        status.textContent = "Nothing is uploaded or sent to a device automatically.";
        closeActionMenu();
        dialog.showModal();
      });
      document.getElementById("plan-export-close").addEventListener("click", () => dialog.close());
      document.getElementById("plan-export-all-dates").addEventListener("click", () => {
        start.value = "";
        end.value = "";
      });
      document.getElementById("plan-export-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const format = document.getElementById("plan-export-format").value;
        const options = { format };
        if (start.value) options.start = start.value;
        if (end.value) options.end = end.value;
        if (start.value && end.value && start.value > end.value) {
          status.textContent = "Choose an end date on or after the start date.";
          return;
        }
        download.disabled = true;
        status.textContent = "Preparing your private plan download…";
        try {
          if (!state.writeToken) await loadConnections();
          if (!state.writeToken) throw new Error("Open the local Training Center server to export a plan.");
          const response = await fetch(PLAN_EXPORT_API, {
            method: "POST",
            headers: apiHeaders({ "content-type": "application/json", "accept": "application/octet-stream" }),
            body: JSON.stringify(options),
            cache: "no-store"
          });
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `Plan export failed (HTTP ${response.status}).`);
          }
          const disposition = response.headers.get("content-disposition") || "";
          const matched = disposition.match(/filename="([A-Za-z0-9._-]+)"/);
          const filename = matched?.[1] || `gradient-ascent-plan.${format}`;
          const blob = await response.blob();
          const url = URL.createObjectURL(blob);
          const anchor = document.createElement("a");
          anchor.href = url;
          anchor.download = filename;
          document.body.appendChild(anchor);
          anchor.click();
          anchor.remove();
          URL.revokeObjectURL(url);
          const entries = Number(response.headers.get("x-gradient-ascent-plan-entries") || 0);
          const fitFiles = Number(response.headers.get("x-gradient-ascent-fit-files") || 0);
          const message = `Downloaded ${entries} planned ${entries === 1 ? "entry" : "entries"}${format === "zip" ? ` and ${fitFiles} device workout${fitFiles === 1 ? "" : "s"}` : ""}.`;
          status.textContent = message;
          setStatus(message);
        } catch (error) {
          status.textContent = error.message || "The plan could not be exported.";
        } finally {
          download.disabled = false;
        }
      });
    }

    function closeActionMenu() {
      const button = document.getElementById("more-actions-button");
      const menu = document.getElementById("more-actions-menu");
      if (!button || !menu) return;
      button.setAttribute("aria-expanded", "false");
      menu.hidden = true;
    }

    function toggleActionMenu() {
      const button = document.getElementById("more-actions-button");
      const menu = document.getElementById("more-actions-menu");
      if (!button || !menu) return;
      const open = menu.hidden;
      button.setAttribute("aria-expanded", String(open));
      menu.hidden = !open;
    }

    function bindSettingsControls() {
      const defaultViewSelect = document.getElementById("default-view-setting");
      const rideSidebarToggle = document.getElementById("ride-sidebar-setting");
      if (defaultViewSelect) {
        defaultViewSelect.value = window.localStorage.getItem(VIEW_STORAGE_KEY) || "weeks";
        defaultViewSelect.addEventListener("change", (event) => {
          window.localStorage.setItem(VIEW_STORAGE_KEY, event.currentTarget.value);
          setStatus("Saved default view.");
        });
      }
      if (rideSidebarToggle) {
        rideSidebarToggle.checked = state.rideSidebarOpen;
        rideSidebarToggle.addEventListener("change", (event) => {
          state.rideSidebarOpen = Boolean(event.currentTarget.checked);
          window.localStorage.setItem(RIDE_SIDEBAR_STORAGE_KEY, String(state.rideSidebarOpen));
          renderRideSidebar();
          setStatus("Saved ride detail preference.");
        });
      }
    }

    function bindCurrentDateLifecycle() {
      window.addEventListener("focus", refreshCurrentDate);
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") refreshCurrentDate();
      });
      window.setInterval(refreshCurrentDate, 60000);
    }

    function bindGlobalControls() {
      const coachButton = document.getElementById("ask-coach-button");
      if (coachButton) coachButton.href = codexThreadUrl(COACH_CONVERSATION_PROMPT);
      document.getElementById("previous-day").addEventListener("click", () => moveDay(-1));
      document.getElementById("jump-to-today").addEventListener("click", () => {
        refreshCurrentDate();
        const anchor = todayAnchorDate();
        if (dayByDate(anchor)) selectDate(anchor, { switchWeek: true, openRide: true });
      });
      document.getElementById("next-day").addEventListener("click", () => moveDay(1));
      document.getElementById("sync-button").addEventListener("click", () => startSync());
      document.getElementById("more-actions-button").addEventListener("click", toggleActionMenu);
      document.getElementById("open-connections").addEventListener("click", () => {
        setView("connections");
        closeActionMenu();
      });
      document.getElementById("open-settings").addEventListener("click", () => {
        setView("settings");
        closeActionMenu();
      });
      document.addEventListener("click", (event) => {
        if (!event.target.closest(".action-menu-wrap")) closeActionMenu();
      });
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeActionMenu();
      });
      window.addEventListener("scroll", syncScrollState, { passive: true });
      document.querySelector(".context-stack")?.addEventListener("scroll", syncScrollState, { passive: true });
      window.addEventListener("resize", () => requestAnimationFrame(syncWeekIntervalLists));
      document.getElementById("today-date-picker").addEventListener("change", (event) => {
        if (!dayByDate(event.currentTarget.value)) {
          event.currentTarget.value = state.selectedDate;
          return;
        }
        selectDate(event.currentTarget.value, { switchWeek: true, openRide: true });
      });
      document.getElementById("calendar-year-select").addEventListener("change", (event) => {
        state.calendarYear = event.currentTarget.value;
        renderCalendar();
        syncNavigationUrl();
      });
    }

    async function loadRuntimeState() {
      await Promise.all([loadNotes(), loadSyncStatus(), loadConnections()]);
      if (state.view === "connections") await loadRideSetupStatus();
      renderCalendar();
      renderWeek();
      renderCoachRail();
      renderTodayDashboard();
      renderMonthRail();
      renderRideSidebar();
      if (state.view === "connections") renderConnections();
    }

    async function init() {
      renderAthleteProfile();
      renderSourceMeta();
      renderTabs();
      renderCalendarYearSelect();
      renderCalendar();
      renderWeekSelect();
      renderWeek();
      renderCoachRail();
      renderTodayDashboard();
      renderMonthRail();
      renderRideSidebar();
      bindImportExport();
      bindActivityRecordingImport();
      bindSettingsControls();
      bindGlobalControls();
      bindCurrentDateLifecycle();
      setView(state.view);
      syncScrollState();
      showFlashStatus();
      void loadRuntimeState();
    }

    init().catch((error) => {
      setStatus(`Training center failed to initialize: ${error.message || error}`);
    });
  </script>
</body>
</html>
"""


def _read(path: Path, default: Any) -> Any:
    payload = read_json(path, default=default)
    return default if payload is None else payload


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _hours_label(value: Any) -> str:
    numeric = _safe_float(value)
    if numeric is None:
        return "0.0h"
    return f"{numeric:.1f}h"


def _daily_hours(seconds: Any) -> float | None:
    numeric = _safe_float(seconds)
    if numeric is None:
        return None
    return round(numeric / 3600.0, 2)


def _target_hours_label(target: dict[str, Any]) -> str:
    min_hours = _safe_float(target.get("min"))
    max_hours = _safe_float(target.get("max"))
    if min_hours is None and max_hours is None:
        return "No target"
    if max_hours is None or max_hours == min_hours:
        return f"{min_hours:g}h"
    if min_hours is None:
        return f"{max_hours:g}h"
    return f"{min_hours:g}-{max_hours:g}h"


def _status_label(status: Any) -> str:
    return {
        "within": "On target",
        "above": "Above target",
        "below": "Below target",
        "not_measured": "Not measured",
        "budget_missing": "Budget not set",
        "budget_review": "Needs review",
        "budget_set": "Budget set",
        "in_progress": "In progress",
        "load_incomplete": "Load incomplete",
        "above_ceiling": "Above ceiling",
        "above_budget": "Above budget",
        "below_budget": "Below budget",
        "within_budget": "Within budget",
    }.get(str(status or ""), str(status or "Tracking").title())


def _status_from_range(
    value: float | None,
    minimum: float | None,
    maximum: float | None,
) -> str | None:
    if value is None or minimum is None or maximum is None:
        return None
    if value < minimum:
        return "below"
    if value > maximum:
        return "above"
    return "within"


def _event_is_skipped(event: dict[str, Any]) -> bool:
    return bool((event.get("markers") or {}).get("skip")) or str(
        event.get("status") or ""
    ).lower() in {"skip", "skipped", "cancelled", "canceled"}


def _event_text(events: list[dict[str, Any]], *, include_skipped: bool = True) -> str:
    parts: list[str] = []
    for event in events:
        if not include_skipped and _event_is_skipped(event):
            continue
        parts.append(
            " ".join(
                str(event.get(key) or "")
                for key in ("name", "discipline", "raw", "location")
            )
        )
    return " ".join(parts)


def _event_kind(events: list[dict[str, Any]], plan_text: str) -> str:
    text = f"{plan_text} {_event_text(events, include_skipped=False)}".lower()
    if not re.search(r"\b(race|crit|criterium|road race|hopper|gravel|mtb|mountain|singletrack|dash|grand prix|giro)\b", text):
        return ""
    if re.search(r"\b(gravel|dirt|mtb|mountain|singletrack|hopper|cx|cyclocross|forest)\b", text):
        return "race_dirt"
    if re.search(r"\b(crit|criterium|grand prix|dash|giro)\b", text):
        return "race_crit"
    return "race_road"


def _normalize_plan_text(text: str) -> str:
    normalized = str(text or "").replace("–", "-").replace("—", "-").replace("×", "x")
    return re.sub(r"\b(ride|workout|session)(?=\d)", r"\1 ", normalized, flags=re.IGNORECASE)


def _strip_interval_durations(text: str) -> str:
    text = _normalize_plan_text(text)
    # Remove rep prescriptions so 4x3min does not become a 3-minute ride.
    return re.sub(
        r"\b\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*"
        r"(?:(?:hours?|hrs?|h)(?:\s*(?:and\s+)?\d+(?:\.\d+)?\s*(?:minutes?|mins?|m))?"
        r"|seconds?|secs?|s|minutes?|mins?|m)?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )


def _duration_to_hours(value: float, unit: str) -> float:
    return value / 60.0 if unit.lower().startswith("m") else value


def _extract_planned_hour_range(
    plan_text: str, *, interval_context: bool = False
) -> tuple[float | None, float | None]:
    text = _strip_interval_durations(plan_text)
    ranges: list[tuple[float, float]] = []

    def whole_session(match: re.Match[str], source: str) -> bool:
        if not interval_context:
            return True
        before, after = source[: match.start()], source[match.end() :]
        # Once reps are present, a remaining "5min easy" or warmup is not
        # evidence for the duration of the entire ride. Require an explicit
        # whole-session context, such as "90min total" or "2h Z2 with ...".
        explicit_total = bool(
            re.search(
                r"\b(?:total|overall|entire)(?:\s+(?:ride|workout|session|time|duration)){0,3}\s*[:=]?\s*$"
                r"|\b(?:ride|workout|session)\s+(?:duration|time)\s*[:=]?\s*$",
                before,
                re.IGNORECASE,
            )
            or re.match(r"\s*(?:total|overall|in\s+total)\b", after, re.IGNORECASE)
        )
        if explicit_total:
            return True
        after_intro = re.split(r"(?<![\w.])\d|[,;]", after, maxsplit=1)[0]
        if re.search(
            r"\b(?:between|recovery|recoveries|warm[- ]?up|cool[- ]?down|rest)\b",
            f"{before.rsplit(',', 1)[-1]} {after_intro}",
            re.IGNORECASE,
        ):
            return False
        return bool(
            re.search(
                r"\b(?:ride|workout|session)(?:\s+(?:for|of))?\s*[:=]?\s*$",
                before,
                re.IGNORECASE,
            )
            or re.match(
                r"\s*(?:(?:easy|steady|controlled|endurance|z[1-7]|zone\s+[1-7]|tempo|threshold|vo2|sweet\s+spot)\s+){0,4}"
                r"(?:ride|workout|session|with|including)\b",
                after,
                re.IGNORECASE,
            )
        )

    number = r"\d+(?:\.\d+)?"
    hour_unit = r"(?:hours?|hrs?|h)"
    minute_unit = r"(?:minutes?|mins?|m)"
    atom = rf"{number}\s*(?:{hour_unit}(?:\s*(?:and\s+)?{number}\s*{minute_unit})?|{minute_unit})\b"

    def atom_hours(value: str) -> float | None:
        hours = re.fullmatch(
            rf"({number})\s*{hour_unit}(?:\s*(?:and\s+)?({number})\s*{minute_unit})?",
            value,
            re.IGNORECASE,
        )
        if hours:
            minutes = float(hours.group(2) or 0)
            result = float(hours.group(1)) + minutes / 60
            return result if minutes < 60 and result <= MAX_DAILY_HOURS else None
        minutes = re.fullmatch(rf"({number})\s*{minute_unit}", value, re.IGNORECASE)
        result = float(minutes.group(1)) / 60 if minutes else None
        return result if result is not None and result <= MAX_DAILY_HOURS else None

    def blank(match: re.Match[str]) -> str:
        return " " * len(match.group())

    # Consume compound/repeated-unit ranges before individual durations so
    # "1h30min-2h" cannot become a 30-minute or two-hour exact prescription.
    repeated_range = re.compile(rf"(?<![\w.+-])({atom})\s*(?:-|to)\s*({atom})", re.IGNORECASE)
    for match in repeated_range.finditer(text):
        start, end = atom_hours(match.group(1)), atom_hours(match.group(2))
        if start is None or end is None or end < start:
            return None, None
        if whole_session(match, text):
            ranges.append((start, end))
    remaining = repeated_range.sub(blank, text)

    range_pattern = re.compile(
        rf"(?<![\w.+-])({number})\s*(?:-|to)\s*({number})\s*({hour_unit}|{minute_unit})\b",
        re.IGNORECASE,
    )
    for match in range_pattern.finditer(remaining):
        start = _duration_to_hours(float(match.group(1)), match.group(3))
        end = _duration_to_hours(float(match.group(2)), match.group(3))
        if end < start or end > MAX_DAILY_HOURS:
            return None, None
        if whole_session(match, remaining):
            ranges.append((start, end))
    remaining = range_pattern.sub(blank, remaining)
    if re.search(rf"(?<![\w.])[+-]\s*{atom}", remaining, re.IGNORECASE):
        return None, None
    single_pattern = re.compile(rf"(?<![\w.+-]){atom}", re.IGNORECASE)
    for match in single_pattern.finditer(remaining):
        hours = atom_hours(match.group())
        if hours is None:
            return None, None
        if whole_session(match, remaining):
            ranges.append((hours, hours))

    if not ranges:
        return None, None
    return max(ranges, key=lambda item: item[1])


def _is_rest_only_plan(plan_text: str) -> bool:
    text = _normalize_plan_text(plan_text).lower()
    if not text:
        return True
    has_rest = re.search(r"\b(off|rest|travel|sick|walk|mobility)\b", text) is not None
    has_active = re.search(r"\b(z1|z2|ride|spin|endurance|tempo|ss|sweet spot|threshold|vo2|race|crit|openers|sprint|hard|easy)\b", text) is not None
    return has_rest and not has_active


def _has_off_option(plan_text: str) -> bool:
    text = _normalize_plan_text(plan_text).lower()
    return " or " in f" {text} " and re.search(r"\b(off|rest|travel)\b", text) is not None


def _planned_intensity_bucket(plan_text: str, events: list[dict[str, Any]]) -> str:
    text = _normalize_plan_text(f"{plan_text} {_event_text(events, include_skipped=False)}").lower()
    race_kind = _event_kind(events, plan_text)
    if race_kind:
        return race_kind
    if re.search(r"\b(vo2|anaerobic|tabata)\b", text) or re.search(r"\b\d+\s*x\s*\d+(?:\.\d+)?\s*min\b.*\bhard\b", text):
        return "vo2"
    if re.search(r"\b(threshold|over.?under|low threshold|z4|zone 4)\b", text):
        return "threshold"
    if re.search(r"\b(ss|sweet spot|low-ss)\b", text):
        return "sweet_spot"
    if "tempo" in text and "not gray-zone tempo" not in text:
        return "tempo"
    if re.search(r"\b\d+\s*x\s*\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*min\b", text):
        return "vo2"
    if re.search(r"\b(openers|sprint|spin-ups?|poppers?|fast|snap|leg opener|cadence)\b", text):
        return "openers"
    if re.search(r"\b(recovery|z1|very easy|cafe|coffee|easy spin)\b", text):
        return "recovery"
    return "endurance"


def _planned_load_label(load: dict[str, Any]) -> str:
    return _planned_load_display(load)["label"]


def _load_number(value: float, *, digits: int = 1) -> str:
    text = f"{value:,.{digits}f}"
    return text.rstrip("0").rstrip(".") if digits else text


def _tss_number(value: float) -> str:
    return f"{math.floor(value + 0.5):,}"


def _planned_load_display(load: dict[str, Any], *, weekly: bool = False) -> dict[str, Any]:
    result = dict(load)
    value = _safe_float(load.get("estimated_tss"))
    low = _safe_float(load.get("estimated_tss_min"))
    high = _safe_float(load.get("estimated_tss_max"))
    if value is None:
        number = "--"
    elif low is not None and high is not None and low != high:
        low_label, high_label = _tss_number(low), _tss_number(high)
        number = low_label if low_label == high_label else f"{low_label}–{high_label}"
    else:
        number = _tss_number(value)
    hours_low = _safe_float(load.get("hours_min"))
    hours_high = _safe_float(load.get("hours_max"))
    if hours_low is None or hours_high is None:
        hours_label = "--"
    elif hours_low == hours_high:
        hours_label = f"{_load_number(hours_low, digits=2)}h"
    else:
        hours_label = f"{_load_number(hours_low, digits=2)}–{_load_number(hours_high, digits=2)}h"
    source = load.get("tss_source")
    qualifier = {
        "source_target": "Source target",
        "explicit_rest": "Rest day",
        "session_if_forecast": "Rough forecast",
        "structured_power_model": "Structured forecast",
        "structured_workout_sum": "Structured forecast",
        "complete_prescribed_sum": "Modeled prescriptions"
        if load.get("estimated")
        else "Prescribed targets",
    }.get(str(source or ""), "Not specified")
    if source == "coach_budget":
        origin = "Coach override" if load.get("budget_override_source") else "Coach budget"
        qualifier = f"{origin} · {load.get('budget_status') or 'provisional'}"
    elif weekly and value is None:
        qualifier = (
            "Budget needs review"
            if load.get("coach_budget_state") in {"needs_review", "orphaned"}
            else "Budget not set"
        )
    ceiling = _safe_float(load.get("budget_ceiling_tss"))
    result.update(
        label=f"{number} planned TSS",
        tss_value_label=f"{number} TSS",
        hours_label=hours_label,
        qualifier=qualifier,
        budget_ceiling_label=_tss_label(ceiling) if ceiling is not None and ceiling >= 0 else None,
        budget_review_required=load.get("coach_budget_state") in {"needs_review", "orphaned"},
        confidence="authored"
        if source == "coach_budget"
        else "estimated"
        if load.get("estimated")
        else "high"
        if value is not None
        else "source_only",
    )
    return result


def _planned_load_for_day(
    plan_text: str,
    events: list[dict[str, Any]],
    *,
    source_load: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(plan_text or "")
    active_events = [event for event in events if not _event_is_skipped(event)]
    has_cancelled_part = bool(re.search(r"\b(cancelled|canceled|skipped)\b", text, re.IGNORECASE))
    discarded_source_total = has_cancelled_part and isinstance(source_load, dict)
    if discarded_source_total:
        source_load = None
    parts = [
        part.strip()
        for part in re.split(r"[;\n]", text)
        if part.strip() and not re.search(r"\b(cancelled|canceled|skipped)\b", part, re.IGNORECASE)
    ]
    if text.strip() and not parts:
        load = day_planned_load()
        load.update(
            intensity="unplanned", note="Canceled or skipped session; no active planned load."
        )
        return _planned_load_display(load)
    cycling_pattern = re.compile(
        r"\b(cycling|bike|biking|ride|spin|z[1-7]|zone [1-7]|threshold|vo2|tempo|ss|sweet spot|openers?|sprints?|race|crit|criterium|gravel|mtb|endurance|recovery|easy)\b",
        re.IGNORECASE,
    )
    noncycling_pattern = re.compile(
        r"\b(run|running|jog|walk|hike|hiking|swim|swimming|strength|weights|gym|yoga|pilates|mobility)\b",
        re.IGNORECASE,
    )
    cycling_events = [
        event
        for event in active_events
        if (
            re.sub(r"[^a-z]", "", str(event.get("discipline") or "").lower())
            in {
                "cycling",
                "road",
                "roadcycling",
                "crit",
                "criterium",
                "gravel",
                "mtb",
                "mountainbike",
                "cyclocross",
                "cx",
                "track",
                "timetrial",
            }
            or (
                not event.get("discipline")
                and re.search(
                    r"\b(cycling|bike|criterium|crit|gravel|mtb|cyclocross|road race|time trial)\b",
                    _event_text([event]),
                    re.IGNORECASE,
                )
            )
        )
    ]
    cycling_parts = [
        part
        for part in parts
        if cycling_pattern.search(part) and not noncycling_pattern.search(part)
    ]
    cycling_context = bool(cycling_parts or cycling_events)
    forecast_parts = (
        [part for part in parts if not noncycling_pattern.search(part)]
        if cycling_context
        else parts
    )
    forecast_text = "; ".join(forecast_parts)
    normalized = _normalize_plan_text(forecast_text).lower()
    intensity = (
        _planned_intensity_bucket(forecast_text, cycling_events) if cycling_context else None
    )
    rest_only = (
        bool(parts)
        and bool(re.search(r"\b(off|rest|travel|sick)\b", normalized))
        and _is_rest_only_plan(forecast_text)
        and not active_events
    )
    if isinstance(source_load, dict):
        hours_min, hours_max = source_load.get("hours_min"), source_load.get("hours_max")
        tss_min, tss_max = source_load.get("tss_min"), source_load.get("tss_max")
        duration_source = "source_duration"
        if any(noncycling_pattern.search(part) for part in parts):
            intensity = None
    else:
        interval_context = _strip_interval_durations(forecast_text) != _normalize_plan_text(
            forecast_text
        )
        durations = [
            bounds
            for part in forecast_parts
            if (bounds := _extract_planned_hour_range(part, interval_context=interval_context))[1]
            is not None
        ]
        hours_min, hours_max = durations[0] if len(durations) == 1 else (None, None)
        if hours_max is not None and _has_off_option(forecast_text):
            hours_min = 0.0
        explicit_tss = re.findall(
            r"(?<![\w.+-])[+-]?[0-9]+(?:\.[0-9]+)?(?:\s*(?:-|–|—|to)\s*[+-]?[0-9]+(?:\.[0-9]+)?)?\s*TSS\b",
            forecast_text,
            re.IGNORECASE,
        )
        tss_min, tss_max = parse_source_range(
            explicit_tss[0] if len(explicit_tss) == 1 else None,
            unit="tss",
            maximum=MAX_DAILY_TSS,
        )
        if explicit_tss and tss_min is None:
            intensity = None
        duration_source = "plan_text"
    load = day_planned_load(
        hours_min=hours_min,
        hours_max=hours_max,
        tss_min=tss_min,
        tss_max=tss_max,
        intensity=intensity,
        is_rest=rest_only,
        duration_source=duration_source,
    )
    load["intensity"] = (
        "rest"
        if rest_only
        else intensity or ("unplanned" if not text.strip() and not active_events else "unspecified")
    )
    if discarded_source_total:
        load["note"] += (
            " The imported day total was not reused because it may include a canceled session."
        )
    return _planned_load_display(load)


def _planned_load_for_week(
    week_days: list[dict[str, Any]],
    *,
    hours_target: dict[str, Any] | None = None,
    tss_target: dict[str, Any] | None = None,
    coach_budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _planned_load_display(
        week_planned_load(
            [day.get("planned_load") or {} for day in week_days],
            hours_target=hours_target,
            tss_target=tss_target,
            coach_budget=coach_budget,
        ),
        weekly=True,
    )


def _structured_dashboard_workouts(
    data_dir: Path,
    athlete: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    if not (data_dir / "plan" / "workouts.json").exists():
        return {}
    by_date: dict[str, list[dict[str, Any]]] = {}
    for workout in load_structured_workouts(data_dir):
        if workout.get("structured") is not True:
            continue
        load = _planned_load_display(structured_workout_load(workout, ftp_w=athlete.get("ftp_w")))
        load["intensity"] = "structured"
        by_date.setdefault(workout["date"], []).append(
            {
                "id": workout["id"],
                "name": workout["name"],
                "load": load,
            }
        )
    return by_date


def _structured_day_load(workouts: list[dict[str, Any]]) -> dict[str, Any]:
    loads = [item["load"] for item in workouts]
    if len(loads) == 1:
        return dict(loads[0])

    def complete_sum(key: str, maximum: float) -> float | None:
        values = [_safe_float(load.get(key)) for load in loads]
        if not values or any(value is None or value < 0 for value in values):
            return None
        total = sum(values)
        return total if total <= maximum else None

    def complete_range(
        low_key: str, high_key: str, maximum: float
    ) -> tuple[float | None, float | None]:
        low, high = complete_sum(low_key, maximum), complete_sum(high_key, maximum)
        return (low, high) if low is not None and high is not None else (None, None)

    hours_min, hours_max = complete_range("hours_min", "hours_max", MAX_DAILY_HOURS)
    tss_min, tss_max = complete_range("estimated_tss_min", "estimated_tss_max", MAX_DAILY_TSS)

    result = day_planned_load(
        hours_min=hours_min,
        hours_max=hours_max,
        tss_min=tss_min,
        tss_max=tss_max,
        duration_source="structured_steps",
    )
    if result["estimated_tss"] is not None:
        result.update(
            estimated_tss=complete_sum("estimated_tss", MAX_DAILY_TSS),
            tss_source="structured_workout_sum",
            method="independent_structured_sum_v1",
            estimated=True,
        )
    result.update(
        intensity="structured",
        note="Sum of independent explicit workouts; no prose session is included.",
    )
    return _planned_load_display(result)


def _week_display_status(
    row: dict[str, Any],
    week_days: list[dict[str, Any]],
    *,
    today: date | None = None,
    planned_load: dict[str, Any] | None = None,
    period: str | None = None,
) -> str:
    plan = row.get("plan") or {}
    load = (
        planned_load
        if planned_load is not None
        else _planned_load_for_week(
            week_days,
            hours_target=row.get("target_hours"),
            tss_target=plan.get("tss_target"),
        )
    )
    target = _safe_float(load.get("estimated_tss"))
    minimum = _safe_float(load.get("estimated_tss_min"))
    maximum = _safe_float(load.get("estimated_tss_max"))
    if (
        target is None
        or minimum is None
        or maximum is None
        or not 0 <= minimum <= target <= maximum
    ):
        return (
            "budget_review"
            if load.get("coach_budget_state") in {"needs_review", "orphaned"}
            else "budget_missing"
        )
    try:
        start_text, end_text = str(row.get("start_date") or ""), str(row.get("end_date") or "")
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        return "budget_missing"
    if start.isoformat() != start_text or end.isoformat() != end_text or end < start:
        return "budget_missing"
    if period is None:
        current = today or date.today()
        period = "future" if current < start else "completed" if current > end else "current"
    if period not in {"future", "current", "completed"}:
        return "budget_missing"
    if period == "future":
        return "budget_set"
    actual_load = _totals_load_display(row.get("totals") or {})
    actual_tss = _safe_float((row.get("totals") or {}).get("estimated_tss"))
    if actual_tss is None or actual_tss < 0:
        return "load_incomplete" if actual_load["tss_missing_activity_count"] else "not_measured"

    def rounded(value: float) -> int:
        return math.floor(value + 0.5)

    ceiling = _safe_float(load.get("budget_ceiling_tss"))
    if ceiling is not None and ceiling >= maximum and rounded(actual_tss) > rounded(ceiling):
        return "above_ceiling"
    if actual_load["tss_partial"]:
        return "load_incomplete"
    if period == "current":
        return "in_progress"
    comparison = _status_from_range(rounded(actual_tss), rounded(minimum), rounded(maximum))
    return {"below": "below_budget", "above": "above_budget", "within": "within_budget"}.get(
        comparison, "budget_missing"
    )


def _week_status_variants(
    row: dict[str, Any], week_days: list[dict[str, Any]], planned_load: dict[str, Any]
) -> dict[str, dict[str, str]]:
    result = {}
    for period in ("future", "current", "completed"):
        status = _week_display_status(row, week_days, planned_load=planned_load, period=period)
        result[period] = {"status": status, "label": _status_label(status)}
    return result


def _event_maps(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_date: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        event_id = str(event.get("id") or "")
        event_date = str(event.get("date") or "")
        if event_id:
            by_id[event_id] = event
        if event_date:
            by_date.setdefault(event_date, []).append(event)
    return by_id, by_date


def _metric_line(metrics: dict[str, Any]) -> str:
    if not metrics.get("activity_count"):
        return "No synced ride yet."
    parts = [
        f"{metrics.get('moving_hours') or 0:g}h moving",
        f"{metrics.get('distance_km') or 0:g} km",
        f"{metrics.get('kilojoules') or 0:g} kJ",
    ]
    if metrics.get("estimated_tss") is not None:
        label = metrics.get("tss_label") or f"{metrics['estimated_tss']:g} TSS"
        qualifier = metrics.get("tss_qualifier")
        parts.append(f"{label}{f' ({qualifier})' if qualifier else ''}")
    if metrics.get("average_heartrate") is not None:
        parts.append(f"HR {round(metrics['average_heartrate'])}")
    return " | ".join(parts)


def _duration_label(seconds: Any) -> str:
    numeric = _safe_float(seconds)
    if not numeric:
        return "0h00"
    minutes = int(round(numeric / 60.0))
    return f"{minutes // 60}h{minutes % 60:02d}"


def _miles_label(meters: Any) -> str:
    numeric = _safe_float(meters) or 0.0
    return f"{numeric / 1609.344:.1f} mi"


def _distance_label(meters: Any) -> str:
    numeric = _safe_float(meters) or 0.0
    return f"{numeric / 1000.0:.1f} km"


def _kj_label(kilojoules: Any) -> str:
    numeric = _safe_float(kilojoules) or 0.0
    return f"{numeric:,.0f} kJ"


def _tss_label(tss: Any) -> str:
    numeric = _safe_float(tss)
    if numeric is None or numeric < 0:
        return "-- TSS"
    return f"{_tss_number(numeric)} TSS"


def _load_display(
    value: Any,
    *,
    estimated: bool = False,
    partial: bool = False,
    description: str = "",
    qualifier: str = "",
    coverage_ratio: float | None = None,
    missing_activity_count: int = 0,
    power_incomplete: bool = False,
) -> dict[str, Any]:
    numeric = _safe_float(value)
    available = numeric is not None and math.isfinite(numeric) and numeric >= 0
    short = _tss_number(numeric) if available else None
    return {
        "tss_label": f"{short} TSS" if short is not None else None,
        "tss_short_label": short,
        "tss_description": description
        if available
        else "No supported power-based load is available."
        + (
            f" {missing_activity_count} cycling activit{'ies are' if missing_activity_count != 1 else 'y is'} missing load."
            if missing_activity_count
            else ""
        ),
        "tss_estimated": bool(available and estimated),
        "tss_partial": bool(available and partial),
        "tss_power_incomplete": bool(available and power_incomplete),
        "tss_qualifier": qualifier
        if available
        else (
            f"{missing_activity_count} ride{'s' if missing_activity_count != 1 else ''} without load"
            if missing_activity_count
            else ""
        ),
        "tss_power_coverage_ratio": coverage_ratio
        if available and coverage_ratio is not None and 0 <= coverage_ratio <= 1
        else None,
        "tss_missing_activity_count": missing_activity_count,
    }


def _power_coverage_label(value: Any, *, partial: bool = False) -> str:
    coverage = _safe_float(value)
    if coverage is None or not math.isfinite(coverage) or not 0 <= coverage <= 1:
        return ""
    percent = coverage * 100
    if partial and coverage < 1:
        percent = min(percent, 99.9)
    return f"{_load_number(percent)}% power coverage"


def _activity_load_display(activity: dict[str, Any]) -> dict[str, Any]:
    source = activity.get("estimated_tss_source")
    estimate = activity.get("power_load_estimate")
    estimate = estimate if isinstance(estimate, dict) else {}
    estimated = source in {"estimated_source_np", "estimated_power_stream"}
    partial = source == "estimated_power_stream" and estimate.get("scope") == "recorded_power"
    coverage = (
        _safe_float(estimate.get("coverage_ratio")) if source == "estimated_power_stream" else None
    )
    coverage_label = _power_coverage_label(coverage, partial=partial) if partial else ""
    if partial:
        duration_basis = (
            "device timer time"
            if estimate.get("reported_duration_source") == "timer_time"
            else "reported moving time"
        )
        coverage_text = (
            f"Recorded power duration is {coverage_label.removesuffix(' power coverage')} of {duration_basis}. "
            if coverage is not None and math.isfinite(coverage) and 0 <= coverage <= 1
            else "Some recorded power is missing. "
        )
        description = (
            f"{coverage_text}Calculated from available power using your currently configured FTP. "
            "Missing power data is not extrapolated."
        )
    elif source == "estimated_power_stream":
        description = "Calculated from recorded power and your currently configured FTP."
    elif source == "estimated_source_np":
        description = "Calculated from source normalized power and your currently configured FTP."
    elif source == "source":
        description = "Training load reported by the activity source."
    else:
        description = "Training load from the imported activity."
    return _load_display(
        activity.get("estimated_tss"),
        estimated=estimated,
        partial=partial,
        description=description,
        qualifier=" · ".join(
            part
            for part in (
                "Calculated" if estimated else "Source",
                coverage_label or ("Power data incomplete" if partial else ""),
            )
            if part
        ),
        coverage_ratio=coverage,
        power_incomplete=partial,
    )


def _totals_load_display(totals: dict[str, Any]) -> dict[str, Any]:
    estimated = (_safe_int(totals.get("estimated_tss_estimated_activity_count")) or 0) > 0
    missing = max(0, _safe_int(totals.get("estimated_tss_missing_activity_count")) or 0)
    partial_streams = _safe_int(totals.get("estimated_tss_relevant_partial_activity_count"))
    if partial_streams is None:
        partial_streams = _safe_int(totals.get("estimated_tss_partial_activity_count")) or 0
    partial = partial_streams > 0 or missing > 0
    coverage = _safe_float(totals.get("estimated_tss_power_coverage_ratio"))
    coverage_label = (
        _power_coverage_label(coverage, partial=partial_streams > 0) if partial_streams > 0 else ""
    )
    missing_label = f"{missing} ride{'s' if missing != 1 else ''} without load" if missing else ""
    qualifiers = ["Calculated" if estimated else "Source"]
    if partial_streams > 0:
        qualifiers.append(coverage_label or "Power data incomplete")
    if missing_label:
        qualifiers.append(missing_label)
    descriptions = [
        "Includes calculated TSS using your currently configured FTP."
        if estimated
        else "Training load reported by the imported activities."
    ]
    if partial_streams > 0:
        descriptions.append(
            f"{coverage_label}, measured as recorded power duration relative to reported activity duration "
            "(device timer time when available, otherwise moving time)."
            if coverage_label
            else "Some recorded power is missing."
        )
    if missing:
        descriptions.append(
            f"{missing} cycling activit{'ies have' if missing != 1 else 'y has'} no supported load value."
        )
    if partial:
        descriptions.append("Missing load is not extrapolated.")
    return _load_display(
        totals.get("estimated_tss"),
        estimated=estimated,
        partial=partial,
        description=" ".join(descriptions),
        qualifier=" · ".join(qualifiers),
        coverage_ratio=coverage,
        missing_activity_count=missing,
        power_incomplete=partial_streams > 0,
    )


def _activity_np_label(activity: dict[str, Any]) -> str | None:
    watts = _safe_float(activity.get("weighted_average_watts"))
    if watts is None or not math.isfinite(watts) or watts < 0:
        return None
    prefix = "~" if activity.get("weighted_average_watts_source") == "estimated_power_stream" else ""
    return f"{prefix}{watts:,.0f} NP"


def _np_label(watts: Any) -> str:
    numeric = _safe_float(watts)
    if not numeric:
        return "-- NP"
    return f"{round(numeric)} NP"


def _hr_label(hr: Any) -> str:
    numeric = _safe_float(hr)
    if not numeric:
        return "HR --"
    return f"HR {round(numeric)}"


def _activity_title_info(
    activity: dict[str, Any], *, planned_name: str | None = None
) -> tuple[str, bool]:
    raw = activity.get("raw") if isinstance(activity.get("raw"), dict) else {}
    private = bool(activity.get("private") or raw.get("private"))
    name = "Private ride" if private else activity.get("name")
    source_ids = (
        activity.get("id"),
        activity.get("provider_id"),
        activity.get("source_activity_id"),
        raw.get("id"),
        raw.get("source_activity_id"),
    )
    baseline = raw.get("source_provider_name")
    authored = not private and (
        activity.get("name_is_authored") is True
        or raw.get("name_is_authored") is True
        or (
            raw.get("source_provider") == "ridewithgps"
            and isinstance(baseline, str)
            and bool(baseline.strip())
            and str(name or "").strip() != baseline.strip()
            and not is_placeholder_title(name, source_ids=source_ids)
        )
    )
    planned = planned_name if planned_name is not None else activity.get("_dashboard_planned_name")
    title = select_activity_title(
        name,
        planned_name=planned,
        authored_title=name if authored else None,
        source_ids=source_ids,
        fallback="Private ride" if private else "Ride",
    )
    from_plan = (
        not authored
        and isinstance(planned, str)
        and title == planned.strip()
        and is_placeholder_title(name, source_ids=source_ids)
        and not is_placeholder_title(title, source_ids=source_ids)
    )
    return title, from_plan


def _activity_label(activity: dict[str, Any], *, planned_name: str | None = None) -> str:
    return _activity_title_info(activity, planned_name=planned_name)[0]


def _optional_kj_label(kilojoules: Any) -> str | None:
    numeric = _safe_float(kilojoules)
    return None if numeric is None else _kj_label(numeric)


def _optional_tss_label(tss: Any) -> str | None:
    numeric = _safe_float(tss)
    return None if numeric is None else _tss_label(numeric)


def _elevation_label(meters: Any) -> str | None:
    numeric = _safe_float(meters)
    if numeric is None:
        return None
    return f"{numeric:,.0f} m climb"


def _suffer_score_label(score: Any) -> str | None:
    numeric = _safe_float(score)
    if numeric is None:
        return None
    return f"{numeric:,.0f} suffer"


def _activity_start_label(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.strftime("%H:%M")


def _safe_int(value: Any) -> int | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def _round_or_none(value: Any, digits: int = 1) -> float | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return round(numeric, digits)


def _watts_label(value: Any, label: str = "W") -> str | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return f"{numeric:,.0f} {label}"


def _if_label(value: Any) -> str | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return f"IF {numeric:.2f}"


def _vi_label(value: Any) -> str | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return f"VI {numeric:.2f}"


def _stress_label(value: Any) -> str | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return f"{numeric:.0f} stress"


def _bpm_label(value: Any) -> str | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return f"{numeric:.0f} bpm"


MEANINGFUL_EXCLUSION_LABELS = {
    "not_ride": "not a ride",
    "no_moving_time": "no moving time",
    "short_ride": "under 30 min",
    "low_tss_ride": "under 10 TSS",
    "low_load_ride": "low-load ride",
}


def _meaningful_exclusion_label(reason: Any) -> str:
    key = str(reason or "").strip()
    if not key:
        return ""
    return MEANINGFUL_EXCLUSION_LABELS.get(key, key.replace("_", " "))


def _day_recovery(
    daily_row: dict[str, Any] | None,
) -> dict[str, Any]:
    recovery = (daily_row or {}).get("primary_recovery")
    if not isinstance(recovery, dict):
        return {
            "available": False,
            "status_label": "Recovery data unavailable for this date",
            "resting_hr_label": None,
            "hrv_label": None,
            "sleep_label": None,
            "sleep_score_label": None,
            "readiness_label": None,
            "stress_label": None,
        }
    provider = str((recovery.get("source") or {}).get("provider") or "recovery")
    provider_label = {
        "apple_health": "Apple Health",
        "garmin": "Garmin Connect",
    }.get(provider, provider.replace("_", " ").title())
    resting_hr = _safe_float(recovery.get("resting_hr"))
    hrv_ms = _safe_float(recovery.get("hrv_ms"))
    sleep_duration_s = _safe_float(recovery.get("sleep_duration_s"))
    sleep_score = _safe_float(recovery.get("sleep_score"))
    readiness_score = _safe_float(recovery.get("readiness_score"))
    stress_avg = _safe_float(recovery.get("stress_avg"))
    return {
        "available": True,
        "provider": provider,
        "status_label": f"{provider_label} recovery for this date",
        "resting_hr": _round_or_none(resting_hr, 1),
        "resting_hr_label": _bpm_label(resting_hr),
        "hrv_ms": _round_or_none(hrv_ms, 1),
        "hrv_label": f"{hrv_ms:.0f} ms" if hrv_ms is not None else None,
        "sleep_duration_s": _round_or_none(sleep_duration_s, 0),
        "sleep_label": _duration_label(sleep_duration_s) if sleep_duration_s else None,
        "sleep_score": _round_or_none(sleep_score, 0),
        "sleep_score_label": f"{sleep_score:.0f}" if sleep_score is not None else None,
        "readiness_score": _round_or_none(readiness_score, 0),
        "readiness_label": f"{readiness_score:.0f}" if readiness_score is not None else None,
        "stress_avg": _round_or_none(stress_avg, 1),
        "stress_label": _stress_label(stress_avg),
    }


def _activity_ids(row: dict[str, Any] | None) -> list[str]:
    if not isinstance(row, dict):
        return []
    ids: list[str] = []
    for value in row.get("activity_ids") or []:
        text = str(value or "").strip()
        if text:
            ids.append(text)
    return ids


def _activity_source_dir(provider: str) -> str:
    return "recordings" if provider == "recording" else provider


@dataclass(frozen=True)
class _ActivityDetailRoot:
    provider: str
    detail_kind: str
    data_root: Path
    path: Path
    descriptor: int | None


_ActivityDetailRoots = dict[tuple[str, str], _ActivityDetailRoot | None]


def _nofollow_read_flags() -> int | None:
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    return os.O_RDONLY | os.O_NOFOLLOW


@contextmanager
def _activity_detail_roots(data_dir: Path):
    data_root = data_dir.expanduser().resolve()
    roots: _ActivityDetailRoots = {
        (provider, detail_kind): None
        for provider in ("strava", "recording")
        for detail_kind in ("laps", "streams")
    }
    supports_anchored_open = (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_follow_symlinks", set())
    )
    if not supports_anchored_open:
        # Reading through a path fallback can follow a swapped parent or final
        # symlink on platforms without openat/O_NOFOLLOW. Keep the dashboard
        # functional, but omit heavy lap/stream details rather than reading
        # outside the private workspace.
        yield roots
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        try:
            data_descriptor = os.open(data_root, flags)
        except OSError:
            yield roots
            return
        descriptors.append(data_descriptor)
        for provider in ("strava", "recording"):
            try:
                provider_descriptor = os.open(
                    _activity_source_dir(provider),
                    flags,
                    dir_fd=data_descriptor,
                )
            except OSError:
                continue
            descriptors.append(provider_descriptor)
            for detail_kind in ("laps", "streams"):
                try:
                    detail_descriptor = os.open(
                        detail_kind,
                        flags,
                        dir_fd=provider_descriptor,
                    )
                except OSError:
                    continue
                descriptors.append(detail_descriptor)
                roots[(provider, detail_kind)] = _ActivityDetailRoot(
                    provider=provider,
                    detail_kind=detail_kind,
                    data_root=data_root,
                    path=(
                        data_root
                        / _activity_source_dir(provider)
                        / detail_kind
                    ),
                    descriptor=detail_descriptor,
                )
        yield roots
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _activity_detail_artifact_name(
    activity_id: int | str,
    provider: str,
) -> str | None:
    if provider not in {"strava", "recording"}:
        return None
    identifier = str(activity_id).strip()
    if (
        not identifier
        or identifier in {".", ".."}
        or Path(identifier).name != identifier
        or "/" in identifier
        or "\\" in identifier
        or "\x00" in identifier
        or (provider == "strava" and not identifier.isdigit())
    ):
        return None
    return f"{identifier}.json"


def _read_activity_detail_artifact(
    data_dir: Path,
    activity_id: int | str,
    provider: str,
    detail_kind: str,
    default: Any,
    *,
    detail_roots: _ActivityDetailRoots | None = None,
) -> Any:
    artifact_name = _activity_detail_artifact_name(activity_id, provider)
    if artifact_name is None or detail_kind not in {"laps", "streams"}:
        return default
    if detail_roots is None:
        with _activity_detail_roots(data_dir) as opened_roots:
            return _read_activity_detail_artifact(
                data_dir,
                activity_id,
                provider,
                detail_kind,
                default,
                detail_roots=opened_roots,
            )
    root = detail_roots.get((provider, detail_kind))
    if root is None or root.descriptor is None:
        return default
    flags = _nofollow_read_flags()
    if flags is None:
        return default
    try:
        descriptor = os.open(
            artifact_name,
            flags,
            dir_fd=root.descriptor,
        )
    except OSError:
        return default
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        if not stat_module.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return default
        payload = json.load(handle)
    return default if payload is None else payload


def _activity_lap_details(
    data_dir: Path,
    activity_id: int | str,
    provider: str = "strava",
    *,
    detail_roots: _ActivityDetailRoots | None = None,
) -> list[dict[str, Any]]:
    payload = _read_activity_detail_artifact(
        data_dir,
        activity_id,
        provider,
        "laps",
        {},
        detail_roots=detail_roots,
    )
    laps = payload.get("laps") if isinstance(payload, dict) else payload
    if not isinstance(laps, list):
        return []
    details: list[dict[str, Any]] = []
    for index, lap in enumerate(laps, start=1):
        if not isinstance(lap, dict):
            continue
        lap_index = _safe_int(lap.get("lap_index") or lap.get("split")) or index
        avg_watts = _safe_float(lap.get("average_watts"))
        np_watts = _safe_float(lap.get("weighted_average_watts"))
        avg_hr = _safe_float(lap.get("average_heartrate"))
        details.append(
            {
                "label": f"Lap {lap_index}",
                "duration_label": _duration_label(lap.get("moving_time") or lap.get("elapsed_time")),
                "hr_label": _hr_label(avg_hr) if avg_hr else None,
                "np_label": _np_label(np_watts) if np_watts else None,
                "avg_watts_label": _watts_label(avg_watts) if avg_watts else None,
                "avg_hr": _round_or_none(avg_hr, 1),
                "np_watts": _round_or_none(np_watts, 0),
                "avg_watts": _round_or_none(avg_watts, 0),
                "moving_time_s": _round_or_none(lap.get("moving_time"), 0),
            }
        )
    details.sort(key=lambda item: _safe_int(str(item.get("label", "")).removeprefix("Lap ")) or 0)
    return details


def _stream_values(payload: dict[str, Any], stream_type: str) -> list[Any]:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("type") != stream_type:
            continue
        values = stream.get("data")
        return values if isinstance(values, list) else []
    return []


def _bucket_stream_values(
    values: list[Any],
    moving: list[Any],
    *,
    max_points: int = 18,
    allow_zero: bool = False,
) -> list[float]:
    samples: list[float] = []
    for index, value in enumerate(values):
        if moving and index < len(moving) and moving[index] is False:
            continue
        numeric = _safe_float(value)
        if numeric is None:
            continue
        if numeric < 0 or (not allow_zero and numeric <= 0):
            continue
        samples.append(numeric)
    if len(samples) < 6:
        return []
    bucket_count = min(max_points, len(samples))
    bucketed: list[float] = []
    for bucket_index in range(bucket_count):
        start = round(bucket_index * len(samples) / bucket_count)
        end = round((bucket_index + 1) * len(samples) / bucket_count)
        chunk = samples[start:end] or samples[start : start + 1]
        if not chunk:
            continue
        bucketed.append(round(sum(chunk) / len(chunk), 1))
    return bucketed


def _minute_bucket_stream_values(
    values: list[Any],
    time_values: list[Any],
    moving: list[Any],
    *,
    allow_zero: bool = False,
) -> list[float]:
    if not values or len(values) != len(time_values):
        return []

    bucketed: dict[int, list[float]] = {}
    start_time: float | None = None
    for index, value in enumerate(values):
        if moving and index < len(moving) and moving[index] is False:
            continue
        numeric = _safe_float(value)
        timestamp = _safe_float(time_values[index])
        if numeric is None or timestamp is None:
            continue
        if numeric < 0 or (not allow_zero and numeric <= 0):
            continue
        if start_time is None:
            start_time = timestamp
        minute_index = max(0, int((timestamp - start_time) // 60))
        bucketed.setdefault(minute_index, []).append(numeric)

    if len(bucketed) < 6:
        return []

    return [
        round(sum(samples) / len(samples), 1)
        for _, samples in sorted(bucketed.items())
        if samples
    ]


def _activity_stream_shape(
    data_dir: Path,
    activity_id: int | str,
    provider: str = "strava",
    *,
    detail_roots: _ActivityDetailRoots | None = None,
) -> dict[str, Any] | None:
    payload = _read_activity_detail_artifact(
        data_dir,
        activity_id,
        provider,
        "streams",
        {},
        detail_roots=detail_roots,
    )
    if not isinstance(payload, dict):
        return None
    moving = _stream_values(payload, "moving")
    time_values = _stream_values(payload, "time")
    minute_altitude = _minute_bucket_stream_values(
        _stream_values(payload, "altitude"),
        time_values,
        moving,
        allow_zero=True,
    )
    altitude = minute_altitude or _bucket_stream_values(
        _stream_values(payload, "altitude"),
        moving,
        allow_zero=True,
    )
    elevation_ft = [round(value * 3.28084, 1) for value in altitude]
    minute_watts = _minute_bucket_stream_values(
        _stream_values(payload, "watts"),
        time_values,
        moving,
        allow_zero=True,
    )
    watts = minute_watts or _bucket_stream_values(
        _stream_values(payload, "watts"),
        moving,
        allow_zero=True,
    )
    if watts and max(watts) > 0:
        return {
            "source": "watts",
            "label": "Minute-average power stream" if minute_watts else "Smoothed power stream",
            "values": watts,
            "elevation_ft": elevation_ft,
        }
    minute_heartrate = _minute_bucket_stream_values(
        _stream_values(payload, "heartrate"),
        time_values,
        moving,
    )
    heartrate = minute_heartrate or _bucket_stream_values(
        _stream_values(payload, "heartrate"),
        moving,
    )
    if heartrate:
        return {
            "source": "heartrate",
            "label": "Minute-average HR stream" if minute_heartrate else "Smoothed HR stream",
            "values": heartrate,
            "elevation_ft": elevation_ft,
        }
    return None


def _activity_annotation_key(activity: dict[str, Any]) -> str:
    activity_id = str(activity.get("id") or "").strip()
    source = activity.get("source") if isinstance(activity.get("source"), dict) else {}
    provider = str(source.get("provider") or "")
    provider_id = str(activity.get("provider_id") or activity_id).strip()
    return provider_id if provider == "strava" else activity_id


def _activity_detail(
    activity: dict[str, Any],
    ride_annotations: dict[str, dict[str, Any]],
    data_dir: Path,
    *,
    include_heavy: bool = True,
    detail_roots: _ActivityDetailRoots | None = None,
) -> dict[str, Any] | None:
    activity_id = str(activity.get("id") or "").strip()
    if not activity_id:
        return None
    source = activity.get("source") if isinstance(activity.get("source"), dict) else {}
    provider = str(source.get("provider") or "")
    provider_id = str(activity.get("provider_id") or activity_id).strip()
    strava_id: int | None = None
    if provider == "strava":
        try:
            strava_id = int(provider_id)
        except (TypeError, ValueError):
            pass
    strava_url = f"https://www.strava.com/activities/{strava_id}" if strava_id is not None else None
    source_url = strava_url
    source_label = "Strava" if strava_url else "Local recording" if provider == "recording" else None
    raw = activity.get("raw") if isinstance(activity.get("raw"), dict) else {}
    ridewithgps_id = str(activity.get("source_activity_id") or raw.get("source_activity_id") or "")
    original_provider = activity.get("source_provider") or raw.get("source_provider")
    if provider == "recording" and original_provider == "ridewithgps" and re.fullmatch(r"[1-9][0-9]{0,31}", ridewithgps_id):
        source_url = f"https://ridewithgps.com/trips/{ridewithgps_id}"
        source_label = "Ride with GPS"
    display_name, name_from_plan = _activity_title_info(activity)
    average_watts = _safe_float(activity.get("average_watts"))
    weighted_watts = _safe_float(activity.get("weighted_average_watts"))
    variability_index = (
        weighted_watts / average_watts
        if weighted_watts is not None and average_watts not in (None, 0)
        else None
    )
    detail = {
        "id": activity_id,
        "name": display_name,
        "name_from_plan": name_from_plan,
        "sport": activity.get("sport_type") or activity.get("type") or "Activity",
        "start_label": _activity_start_label(activity.get("start_date_local") or activity.get("start_date")),
        "strava_url": strava_url,
        "source_url": source_url,
        "source_label": source_label,
        "duration_label": _duration_label(activity.get("moving_time_s")),
        "miles_label": _miles_label(activity.get("distance_m")),
        "distance_label": _distance_label(activity.get("distance_m")),
        "kilojoules_label": _optional_kj_label(activity.get("kilojoules")),
        **_activity_load_display(activity),
        "np_label": _activity_np_label(activity),
        "np_description": (
            "Estimated normalized power from recorded power samples."
            if activity.get("weighted_average_watts_source") == "estimated_power_stream"
            else "Normalized power reported by the activity source."
        ),
        "hr_label": _hr_label(activity.get("average_heartrate")) if _safe_float(activity.get("average_heartrate")) else None,
        "elevation_label": _elevation_label(activity.get("elevation_gain_m")),
        "suffer_score_label": _suffer_score_label(activity.get("suffer_score")),
        "if_label": _if_label(activity.get("intensity_factor")),
        "avg_watts_label": _watts_label(average_watts),
        "vi_label": _vi_label(variability_index),
        "moving_hours": _daily_hours(activity.get("moving_time_s")),
        "distance_km": _round_or_none((_safe_float(activity.get("distance_m")) or 0.0) / 1000.0, 1) if activity.get("distance_m") is not None else None,
        "kilojoules": _round_or_none(activity.get("kilojoules"), 0),
        "estimated_tss": _round_or_none(activity.get("estimated_tss"), 1),
        "np_watts": _round_or_none(activity.get("weighted_average_watts"), 0),
        "avg_hr": _round_or_none(activity.get("average_heartrate"), 1),
        "elevation_m": _round_or_none(activity.get("elevation_gain_m"), 0),
        "suffer_score": _round_or_none(activity.get("suffer_score"), 0),
        "meaningful": bool(activity.get("is_meaningful_ride")),
        "exclusion_reason": _meaningful_exclusion_label(activity.get("meaningful_exclusion_reason")),
    }
    detail.update(ride_annotations.get(_activity_annotation_key(activity), {}))
    if not include_heavy:
        return detail
    detail_source = provider if provider in {"strava", "recording"} else None
    detail_record_id: int | str | None = strava_id if provider == "strava" else provider_id
    detail["laps"] = (
        _activity_lap_details(
            data_dir,
            detail_record_id,
            detail_source,
            detail_roots=detail_roots,
        )
        if detail_source and detail_record_id is not None
        else []
    )
    stream_shape = (
        _activity_stream_shape(
            data_dir,
            detail_record_id,
            detail_source,
            detail_roots=detail_roots,
        )
        if detail_source and detail_record_id is not None
        else None
    )
    if stream_shape:
        detail["stream_shape"] = stream_shape
    if not detail.get("lap_count"):
        detail["lap_count"] = len(detail["laps"])
        detail["lap_count_label"] = f"{len(detail['laps'])} laps" if detail["laps"] else None
    return detail


def _activity_details(
    daily_row: dict[str, Any] | None,
    activities_by_id: dict[str, dict[str, Any]],
    ride_annotations: dict[str, dict[str, Any]],
    data_dir: Path,
    *,
    include_heavy: bool = True,
    detail_roots: _ActivityDetailRoots | None = None,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for activity_id in _activity_ids(daily_row):
        activity = activities_by_id.get(activity_id)
        if not activity:
            continue
        detail = _activity_detail(
            activity,
            ride_annotations,
            data_dir,
            include_heavy=include_heavy,
            detail_roots=detail_roots,
        )
        if detail:
            details.append(detail)
    return details


def _activity_summary(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep overview fields in the boot payload; defer heavy chart and lap arrays."""
    return {
        key: value
        for key, value in detail.items()
        if key not in {"laps", "stream_shape"}
    }


def _activity_detail_file_signature(
    activity_id: int | str,
    provider: str,
    detail_kind: str,
    detail_roots: _ActivityDetailRoots,
) -> dict[str, int] | None:
    artifact_name = _activity_detail_artifact_name(activity_id, provider)
    if artifact_name is None:
        return None
    root = detail_roots.get((provider, detail_kind))
    if root is None or root.descriptor is None:
        return None
    try:
        file_stat = os.stat(
            artifact_name,
            dir_fd=root.descriptor,
            follow_symlinks=False,
        )
    except (OSError, TypeError):
        return None
    if not stat_module.S_ISREG(file_stat.st_mode):
        return None
    return {
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "ctime_ns": file_stat.st_ctime_ns,
    }


def _activity_detail_cache_input(
    activity: dict[str, Any],
    ride_annotations: dict[str, dict[str, Any]],
    detail_roots: _ActivityDetailRoots,
) -> dict[str, Any]:
    activity_id = str(activity.get("id") or "").strip()
    source = activity.get("source") if isinstance(activity.get("source"), dict) else {}
    provider = str(source.get("provider") or "")
    provider_id = str(activity.get("provider_id") or activity_id).strip()
    detail_files: dict[str, dict[str, int] | None] = {}
    if provider in {"strava", "recording"}:
        for detail_kind in ("laps", "streams"):
            detail_files[detail_kind] = _activity_detail_file_signature(
                provider_id,
                provider,
                detail_kind,
                detail_roots,
            )
    return {
        "activity": activity,
        "annotation": ride_annotations.get(_activity_annotation_key(activity), {}),
        "detail_files": detail_files,
    }


def _activity_detail_week_fingerprint(
    day_activity_ids: dict[str, list[str]],
    activities_by_id: dict[str, dict[str, Any]],
    ride_annotations: dict[str, dict[str, Any]],
    data_dir: Path,
) -> str:
    with _activity_detail_roots(data_dir) as detail_roots:
        fingerprint_input = {
            "version": ACTIVITY_DETAILS_CACHE_VERSION,
            "days": [
                {
                    "date": day_key,
                    "activities": [
                        _activity_detail_cache_input(
                            activities_by_id[activity_id],
                            ride_annotations,
                            detail_roots,
                        )
                        for activity_id in activity_ids
                        if activity_id in activities_by_id
                    ],
                }
                for day_key, activity_ids in day_activity_ids.items()
            ],
        }
    serialized = json.dumps(
        fingerprint_input,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _load_activity_detail_manifest(
    path: Path,
    *,
    directory_descriptor: int | None = None,
) -> dict[str, dict[str, Any]] | None:
    body = _read_regular_file_bytes(path, directory_descriptor=directory_descriptor)
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != ACTIVITY_DETAILS_CACHE_VERSION:
        return None
    weeks = payload.get("weeks")
    if not isinstance(weeks, dict):
        return None
    loaded: dict[str, dict[str, Any]] = {}
    for week_start, entry in weeks.items():
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("fingerprint"), str)
            and isinstance(entry.get("file"), str)
            and isinstance(entry.get("size"), int)
            and isinstance(entry.get("sha256"), str)
        ):
            return None
        loaded[str(week_start)] = entry
    return loaded


def _build_week_activity_details(
    day_activity_ids: dict[str, list[str]],
    activities_by_id: dict[str, dict[str, Any]],
    ride_annotations: dict[str, dict[str, Any]],
    data_dir: Path,
) -> dict[str, list[dict[str, Any]]]:
    details: dict[str, list[dict[str, Any]]] = {}
    with _activity_detail_roots(data_dir) as detail_roots:
        for day_key, activity_ids in day_activity_ids.items():
            day_details = _activity_details(
                {"activity_ids": activity_ids},
                activities_by_id,
                ride_annotations,
                data_dir,
                detail_roots=detail_roots,
            )
            if day_details:
                details[day_key] = day_details
    return details


def _activity_detail_script(
    week_start: str,
    fingerprint: str,
    week_details: dict[str, list[dict[str, Any]]],
) -> str:
    return (
        "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ = "
        "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ || {};\n"
        "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__["
        + json.dumps(week_start)
        + "] = "
        + json.dumps(
            {"cache_key": fingerprint, "days": week_details},
            separators=(",", ":"),
        )
        + ";\n"
    )


def _activity_detail_lap_counts(
    week_details: dict[str, list[dict[str, Any]]],
) -> dict[str, list[int]]:
    return {
        day_key: [
            _safe_int(detail.get("lap_count")) or 0
            for detail in activity_details
        ]
        for day_key, activity_details in week_details.items()
    }


def _cached_activity_detail_lap_counts(
    body: bytes,
    *,
    week_start: str,
    fingerprint: str,
) -> dict[str, list[int]] | None:
    assignment_prefix = (
        "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ = "
        "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ || {};\n"
        "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__["
        + json.dumps(week_start)
        + "] = "
    )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.startswith(assignment_prefix) or not text.endswith(";\n"):
        return None
    try:
        payload = json.loads(text[len(assignment_prefix) : -2])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("cache_key") != fingerprint:
        return None
    days = payload.get("days")
    if not isinstance(days, dict):
        return None
    if any(
        not isinstance(details, list)
        or any(not isinstance(detail, dict) for detail in details)
        for details in days.values()
    ):
        return None
    return _activity_detail_lap_counts(days)


def _synced_activity_summary(
    daily_row: dict[str, Any] | None,
    activities_by_id: dict[str, dict[str, Any]],
    fallback_metric_line: str,
) -> dict[str, Any]:
    activity_ids = _activity_ids(daily_row)
    rides = [activities_by_id[activity_id] for activity_id in activity_ids if activity_id in activities_by_id]
    rides.sort(key=lambda item: str(item.get("start_date_local") or item.get("start_date") or ""))
    if not rides:
        assessment = fallback_metric_line
        return {
            "summary": fallback_metric_line,
            "assessment": assessment,
            "actual": fallback_metric_line,
            "actual_title_from_plan": False,
            "has_synced_ride": bool(((daily_row or {}).get("totals") or {}).get("activity_count")),
            "hard_activity": False,
        }

    totals = (daily_row or {}).get("totals") or {}
    load_display = _totals_load_display(totals)
    load_label = load_display["tss_label"] or "-- TSS"
    if load_display["tss_qualifier"]:
        load_label += f" ({load_display['tss_qualifier']})"
    duration = _duration_label(totals.get("moving_time_s"))
    if totals.get("excluded_short_ride_time_s"):
        duration = f"{duration} ({_duration_label(totals.get('meaningful_ride_time_s'))} meaningful)"

    if len(rides) == 1:
        anchor_ride = rides[0]
        title, title_from_plan = _activity_title_info(anchor_ride)
        actual_title = title
    else:
        anchor_ride = max(rides, key=lambda ride: float(ride.get("moving_time_s") or 0.0))
        actual_title, title_from_plan = _activity_title_info(anchor_ride)
        title = f"{len(rides)} rides"

    assessment = (
        f"{duration} | {_distance_label(totals.get('distance_m'))} | "
        f"{_kj_label(totals.get('kilojoules'))} | {load_label} | "
        f"{_activity_np_label(anchor_ride) or '-- NP'} | "
        f"{_hr_label(totals.get('average_heartrate'))}. {title}"
    )
    summary = f"Actual: {assessment}"
    suffer_score = max(
        [_safe_float(ride.get("suffer_score")) or 0.0 for ride in rides]
        or [0.0]
    )
    return {
        "summary": summary,
        "assessment": assessment,
        "actual": actual_title,
        "actual_title_from_plan": title_from_plan,
        "has_synced_ride": True,
        "hard_activity": suffer_score >= 60.0,
        "anchor_activity": title,
    }


def _is_planned_hard_day(plan_text: str) -> bool:
    lowered = plan_text.lower()
    if any(token in lowered for token in ("vo2", "2x20", "3x15", "3x12", "threshold")):
        return True
    if "tempo" not in lowered:
        return False
    if "not gray-zone tempo" in lowered:
        return False
    return not ("no intervals" in lowered or "easy" in lowered)


def _latest_week_ride(week_row: dict[str, Any], activities_by_id: dict[str, dict[str, Any]]) -> str:
    rides = [
        activities_by_id[activity_id]
        for activity_id in _activity_ids(week_row)
        if activity_id in activities_by_id
    ]
    if not rides:
        return "No rides synced yet for this week."
    latest = max(rides, key=lambda ride: str(ride.get("start_date_local") or ride.get("start_date") or ""))
    title = _activity_label(latest)
    return (
        f"Latest ride: {title} - {_duration_label(latest.get('moving_time_s'))}, "
        f"{_distance_label(latest.get('distance_m'))}, {_kj_label(latest.get('kilojoules'))}."
    )


def _progress_pct(value: Any, maximum: Any) -> float:
    numeric = _safe_float(value) or 0.0
    max_numeric = _safe_float(maximum) or 0.0
    if not max_numeric:
        return 0.0
    return round(min(100.0, 100.0 * numeric / max_numeric), 1)


def _day_metrics(daily_row: dict[str, Any] | None) -> dict[str, Any]:
    totals = (daily_row or {}).get("totals") or {}
    moving_hours = _daily_hours(totals.get("moving_time_s"))
    meaningful_hours = _daily_hours(totals.get("meaningful_ride_time_s"))
    distance = _safe_float(totals.get("distance_m"))
    kilojoules = _safe_float(totals.get("kilojoules"))
    estimated_tss = _safe_float(totals.get("estimated_tss"))
    average_heartrate = _safe_float(totals.get("average_heartrate"))
    return {
        "activity_count": int(totals.get("activity_count") or 0),
        "moving_hours": moving_hours,
        "meaningful_ride_hours": meaningful_hours,
        "distance_km": round(distance / 1000.0, 1) if distance is not None else None,
        "kilojoules": round(kilojoules, 0) if kilojoules is not None else None,
        "estimated_tss": round(estimated_tss, 1) if estimated_tss is not None else None,
        **_totals_load_display(totals),
        "tss_power_load_duration_s": _safe_float(totals.get("estimated_tss_power_load_duration_s")),
        "tss_power_reported_duration_s": _safe_float(totals.get("estimated_tss_power_reported_duration_s")),
        "average_heartrate": average_heartrate,
        "by_sport": totals.get("by_sport") or {},
    }


def _load_daily_notes(data_dir: Path) -> dict[str, Any]:
    notes_path = data_dir / "plan" / "daily_notes.json"
    if not notes_path.exists():
        return {}
    payload = _read(notes_path, {})
    notes = payload.get("notes") if isinstance(payload, dict) else {}
    return notes if isinstance(notes, dict) else {}


def _goals_summary(data_dir: Path) -> dict[str, str | None]:
    path = data_dir / "plan" / "goals.md"
    if not path.exists():
        return {"north_star": None, "primary_goal": None}
    north_star = None
    primary_goal = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# ") and north_star is None:
            candidate = line[2:].strip()
            if not candidate.lower().startswith("replace with"):
                north_star = candidate
        elif line.startswith("### ") and primary_goal is None:
            candidate = line[4:].strip()
            if not candidate.lower().startswith("replace with"):
                primary_goal = candidate
        if north_star and primary_goal:
            break
    return {"north_star": north_star, "primary_goal": primary_goal}


def _build_payload(
    data_dir: Path,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    derived_dir = data_dir / "derived"
    weekly_rows = _read(derived_dir / "weekly.json", [])
    daily_rows = _read(derived_dir / "daily.json", [])
    activities = _read(derived_dir / "activities.json", [])
    events = _read(data_dir / "plan" / "events.json", [])
    phases = _read(data_dir / "plan" / "phases.json", [])
    athlete = _read(data_dir / "plan" / "athlete.json", {})
    athlete = athlete if isinstance(athlete, dict) else {}
    coach_budgets = load_tss_budgets(data_dir)
    structured_by_date = _structured_dashboard_workouts(data_dir, athlete)
    weekly_rows = list(weekly_rows) if isinstance(weekly_rows, list) else []
    covered_ranges = [
        (str(row.get("start_date") or ""), str(row.get("end_date") or ""))
        for row in weekly_rows
        if isinstance(row, dict)
    ]

    def covered(day_key: str) -> bool:
        return any(start_key <= day_key <= end_key for start_key, end_key in covered_ranges)

    for day_key in structured_by_date:
        if covered(day_key):
            continue
        day_date = date.fromisoformat(day_key)
        monday = day_date - timedelta(days=day_date.weekday())
        sunday = monday + timedelta(days=6)
        week_start = week_end = day_date
        while week_start > monday and not covered((week_start - timedelta(days=1)).isoformat()):
            week_start -= timedelta(days=1)
        while week_end < sunday and not covered((week_end + timedelta(days=1)).isoformat()):
            week_end += timedelta(days=1)
        covered_ranges.append((week_start.isoformat(), week_end.isoformat()))
        weekly_rows.append(
            {
                "start_date": week_start.isoformat(),
                "end_date": week_end.isoformat(),
                "plan": {
                    "days": {},
                    "phase": "Structured workouts",
                    "primary_focus": "Scheduled workouts",
                },
                "target_hours": {},
                "actual_hours": 0,
                "totals": {"activity_count": 0, "moving_time_s": 0},
            }
        )
    weekly_rows.sort(
        key=lambda row: str(row.get("start_date") or "") if isinstance(row, dict) else ""
    )
    post_sync_summary = _read(derived_dir / "post_sync_summary.json", {})
    day_labels = day_labels_by_date(data_dir)
    ride_annotations = ride_annotations_by_id(data_dir)
    event_by_id, events_by_date = _event_maps(events if isinstance(events, list) else [])
    daily_map = {row["date"]: row for row in daily_rows if isinstance(row, dict) and row.get("date")}
    activities_by_id: dict[str, dict[str, Any]] = {}
    for row in activities:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        activities_by_id[str(row["id"])] = dict(row)

    weeks: list[dict[str, Any]] = []
    days: list[dict[str, Any]] = []
    activity_ids_by_week: dict[str, dict[str, list[str]]] = {}
    for row in weekly_rows:
        if not isinstance(row, dict) or not row.get("start_date") or not row.get("end_date"):
            continue
        plan = row.get("plan") or {}
        target = row.get("target_hours") or {}
        target_max = (target or {}).get("max")
        target_min = (target or {}).get("min")
        totals = row.get("totals") or {}
        week_events = [
            event_by_id[event_id]
            for event_id in (plan.get("events") or [])
            if event_id in event_by_id
        ]
        actual = plan.get("actual") or {}
        start = date.fromisoformat(row["start_date"])
        week_days = []
        plan_days = plan.get("days") or {}
        planned_day_inputs: list[tuple[str, str, list[dict[str, Any]]]] = []
        day_loads = plan.get("day_loads") if isinstance(plan.get("day_loads"), dict) else {}
        day_count = min(7, max(0, (date.fromisoformat(row["end_date"]) - start).days + 1))
        for index in range(day_count):
            day_date = start + timedelta(days=index)
            day_key = day_date.isoformat()
            weekday = WEEKDAYS[day_date.weekday()]
            planned = plan_days.get(weekday) or ""
            planned_day_inputs.append((weekday, planned, events_by_date.get(day_key, [])))
        for index, (weekday, planned, day_events) in enumerate(planned_day_inputs):
            day_date = start + timedelta(days=index)
            day_key = day_date.isoformat()
            independent_workouts = structured_by_date.get(day_key, [])
            source_load = day_loads.get(weekday)
            structured_is_primary = (
                bool(independent_workouts)
                and not str(planned).strip()
                and not isinstance(source_load, dict)
            )
            planned_load = (
                _structured_day_load(independent_workouts)
                if structured_is_primary
                else _planned_load_for_day(planned, day_events, source_load=source_load)
            )
            if structured_is_primary:
                planned = "; ".join(item["name"] for item in independent_workouts)
            daily_row = daily_map.get(day_key)
            for activity_id in _activity_ids(daily_row):
                if activity_id in activities_by_id:
                    activities_by_id[activity_id]["_dashboard_planned_name"] = planned
            metrics = _day_metrics(daily_row)
            metric_line = _metric_line(metrics)
            synced_summary = _synced_activity_summary(
                daily_row,
                activities_by_id,
                metric_line,
            )
            source_note = _weekday_value(actual, weekday) or ""
            planned_intensity = str(planned_load.get("intensity") or "")
            day_activity_ids = [
                activity_id
                for activity_id in _activity_ids(daily_row)
                if activity_id in activities_by_id
            ]
            activity_summaries = _activity_details(
                daily_row,
                activities_by_id,
                ride_annotations,
                data_dir,
                include_heavy=False,
            )
            if activity_summaries:
                activity_ids_by_week.setdefault(row["start_date"], {})[day_key] = (
                    day_activity_ids
                )
            day_record = {
                "date": day_key,
                "weekday": weekday,
                "week_start": row["start_date"],
                "week_end": row["end_date"],
                "week_focus": plan.get("primary_focus") or "",
                "phase": plan.get("phase") or "",
                "planned": planned,
                "actual": synced_summary["actual"],
                "actual_title_from_plan": synced_summary["actual_title_from_plan"],
                "source_note": source_note,
                "events": day_events,
                "metrics": metrics,
                "planned_load": planned_load,
                "structured_workouts": independent_workouts,
                "structured_is_primary": structured_is_primary,
                "metric_line": metric_line,
                "activity_summary": synced_summary["summary"],
                "activities": [
                    _activity_summary(detail) for detail in activity_summaries
                ],
                "dashboard_labels": day_labels.get(day_key, []),
                "recovery": _day_recovery(daily_row),
                "has_synced_ride": synced_summary["has_synced_ride"],
                "hard_activity": synced_summary["hard_activity"],
                "planned_hard_day": _is_planned_hard_day(planned)
                or planned_intensity in {"sweet_spot", "threshold", "vo2"},
            }
            week_days.append(day_record)
            days.append(day_record)

        meaningful_hours = row.get("meaningful_ride_hours")
        if meaningful_hours is None:
            meaningful_hours = row.get("actual_hours")
        coach_budget = coach_budgets.get((row["start_date"], row["end_date"]))
        planned_week_load = _planned_load_for_week(
            week_days,
            hours_target=target,
            tss_target=plan.get("tss_target"),
            coach_budget=coach_budget,
        )
        display_status = _week_display_status(row, week_days, planned_load=planned_week_load)
        week_load = _totals_load_display(totals)
        week_record = {
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "range_label": row.get("range_label") or "",
            "phase": plan.get("phase") or "",
            "primary_focus": plan.get("primary_focus") or "",
            "notes": plan.get("notes") or "",
            "why_logic": (plan.get("raw") or {}).get("Why / logic") or "",
            "strength_rehab": plan.get("strength_rehab") or "",
            "target_hours": target,
            "target_hours_label": _target_hours_label(target),
            "actual_hours": row.get("actual_hours"),
            "actual_hours_label": _hours_label(row.get("actual_hours")),
            "meaningful_ride_hours": meaningful_hours,
            "meaningful_ride_hours_label": _hours_label(meaningful_hours),
            "excluded_short_ride_hours": row.get("excluded_short_ride_hours"),
            "excluded_short_ride_hours_label": _hours_label(row.get("excluded_short_ride_hours")),
            "progress_pct": _progress_pct(meaningful_hours, target_max),
            "target_min_pct": _progress_pct(target_min, target_max),
            "kilojoules_label": _kj_label(totals.get("kilojoules")),
            **week_load,
            "estimated_tss_label": week_load["tss_label"] or "-- TSS",
            "distance_km": _round_or_none(
                (_safe_float(totals.get("distance_m")) or 0.0) / 1000.0,
                1,
            )
            if totals.get("distance_m") is not None
            else None,
            "planned_load": planned_week_load,
            "coach_budget": {
                key: coach_budget[key]
                for key in (
                    "state",
                    "status",
                    "target_tss",
                    "range",
                    "ceiling_tss",
                    "rationale",
                    "conditions",
                    "revision",
                    "override_source",
                )
                if key in coach_budget
            }
            if isinstance(coach_budget, dict)
            else None,
            "separate_structured_workout_count": sum(
                len(day["structured_workouts"])
                for day in week_days
                if not day["structured_is_primary"]
            ),
            "latest_ride": _latest_week_ride(row, activities_by_id),
            "execution_note": (actual or {}).get("Tactical notes / sources") or "",
            "status": display_status,
            "status_label": _status_label(display_status),
            "status_by_period": _week_status_variants(row, week_days, planned_week_load),
            "totals": totals,
            "events": week_events,
            "has_activity_details": bool(activity_ids_by_week.get(row["start_date"])),
        }
        weeks.append(week_record)

    payload = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "athlete": athlete if isinstance(athlete, dict) else {},
        "postSyncSummary": post_sync_summary,
        "phases": phases if isinstance(phases, list) else [],
        "events": events if isinstance(events, list) else [],
        "weeks": weeks,
        "days": days,
        "notes": _load_daily_notes(data_dir),
        "coachNotes": coach_notes_by_date(data_dir),
        "goals": _goals_summary(data_dir),
        "onboarding": _read(data_dir / "plan" / "onboarding.json", {"choices": {}}),
        "workspacePath": str(data_dir.expanduser().resolve()),
    }
    return payload, activity_ids_by_week, activities_by_id, ride_annotations


def _read_regular_file_bytes(
    path: Path,
    *,
    expected_size: int | None = None,
    directory_descriptor: int | None = None,
) -> bytes | None:
    flags = _nofollow_read_flags()
    if flags is None:
        # A cache miss is safe and only costs a sidecar rebuild. Following a
        # final-component symlink on a platform without O_NOFOLLOW is not.
        return None
    try:
        if directory_descriptor is None:
            descriptor = os.open(path, flags)
        else:
            descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        try:
            file_stat = os.fstat(handle.fileno())
        except OSError:
            return None
        if (
            not stat_module.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_size > MAX_ACTIVITY_DETAIL_SIDECAR_BYTES
            or (expected_size is not None and file_stat.st_size != expected_size)
        ):
            return None
        body = handle.read(file_stat.st_size + 1)
    return body if len(body) == file_stat.st_size else None


def _legacy_activity_detail_manifest(data_dir: Path) -> dict[str, dict[str, Any]] | None:
    root = data_dir.expanduser().resolve()
    codex_dir = root / ".codex"
    cache_dir = codex_dir / "cache"
    for directory in (codex_dir, cache_dir):
        if directory.is_symlink():
            raise ValueError(f"Training Center output directory cannot be a symlink: {directory}")
    if not (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
    ):
        return None

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        try:
            descriptors.append(os.open(root, flags))
            descriptors.append(os.open(".codex", flags, dir_fd=descriptors[-1]))
            descriptors.append(os.open("cache", flags, dir_fd=descriptors[-1]))
        except OSError as exc:
            for directory in (codex_dir, cache_dir):
                if directory.is_symlink():
                    raise ValueError(
                        f"Training Center output directory cannot be a symlink: {directory}"
                    ) from exc
            return None
        return _load_activity_detail_manifest(
            cache_dir / "training_center_activity_details.json",
            directory_descriptor=descriptors[-1],
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _cached_activity_detail_matches(
    path: Path,
    entry: dict[str, Any] | None,
    *,
    week_start: str,
    fingerprint: str,
    filename: str,
) -> dict[str, list[int]] | None:
    if not entry:
        return None
    if entry.get("fingerprint") != fingerprint or entry.get("file") != filename:
        return None
    body = _read_regular_file_bytes(
        path,
        expected_size=entry.get("size"),
    )
    if body is None:
        return None
    if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
        return None
    return _cached_activity_detail_lap_counts(
        body,
        week_start=week_start,
        fingerprint=fingerprint,
    )


def _prepare_activity_detail_sidecars(
    data_dir: Path,
    activity_details_dir: Path,
    activity_ids_by_week: dict[str, dict[str, list[str]]],
    activities_by_id: dict[str, dict[str, Any]],
    ride_annotations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    safe_activity_details_dir = _ensure_private_output_directory(
        data_dir,
        Path("derived") / "training_center_activity_details",
    )
    if activity_details_dir.expanduser().resolve() != safe_activity_details_dir:
        raise ValueError("Activity detail output directory did not match the workspace.")
    activity_details_dir = safe_activity_details_dir
    cache_dir = _ensure_private_output_directory(
        data_dir,
        Path("derived") / ".cache",
    )
    manifest_path = cache_dir / "training_center_activity_details.json"
    loaded_previous_weeks = _load_activity_detail_manifest(manifest_path)
    if loaded_previous_weeks is None:
        loaded_previous_weeks = _legacy_activity_detail_manifest(data_dir)
    manifest_was_valid = loaded_previous_weeks is not None
    previous_weeks = loaded_previous_weeks or {}
    existing_files_before = {
        path.name
        for path in activity_details_dir.glob("*.js")
        if not path.is_symlink() and path.is_file()
    }
    current_weeks: dict[str, dict[str, Any]] = {}
    files_by_week: dict[str, str] = {}
    lap_counts_by_week: dict[str, dict[str, list[int]]] = {}
    rebuilt = 0
    reused = 0

    for week_start, day_activity_ids in activity_ids_by_week.items():
        accepted: tuple[
            str,
            str,
            Path,
            dict[str, list[int]],
            bool,
            bytes | None,
        ] | None = None
        for _attempt in range(3):
            fingerprint = _activity_detail_week_fingerprint(
                day_activity_ids,
                activities_by_id,
                ride_annotations,
                data_dir,
            )
            filename = f"{week_start}.{fingerprint}.js"
            detail_path = activity_details_dir / filename
            previous_entry = previous_weeks.get(week_start)
            cached_lap_counts = _cached_activity_detail_matches(
                detail_path,
                previous_entry,
                week_start=week_start,
                fingerprint=fingerprint,
                filename=filename,
            )
            cache_hit = cached_lap_counts is not None
            generated_body: bytes | None = None
            if cache_hit:
                lap_counts = cached_lap_counts
            else:
                week_details = _build_week_activity_details(
                    day_activity_ids,
                    activities_by_id,
                    ride_annotations,
                    data_dir,
                )
                lap_counts = _activity_detail_lap_counts(week_details)
                script = _activity_detail_script(
                    week_start,
                    fingerprint,
                    week_details,
                )
                generated_body = script.encode("utf-8")
                write_text(detail_path, script)
            verified_fingerprint = _activity_detail_week_fingerprint(
                day_activity_ids,
                activities_by_id,
                ride_annotations,
                data_dir,
            )
            if verified_fingerprint == fingerprint:
                accepted = (
                    fingerprint,
                    filename,
                    detail_path,
                    lap_counts,
                    cache_hit,
                    generated_body,
                )
                break
        if accepted is None:
            raise RuntimeError(
                f"Ride details for week {week_start} changed repeatedly during refresh; retry."
            )
        (
            fingerprint,
            filename,
            detail_path,
            lap_counts,
            cache_hit,
            generated_body,
        ) = accepted
        reused += int(cache_hit)
        rebuilt += int(not cache_hit)
        body = generated_body or _read_regular_file_bytes(detail_path)
        if body is None:
            raise RuntimeError(f"Could not verify ride details for week {week_start}.")
        current_weeks[week_start] = {
            "fingerprint": fingerprint,
            "file": filename,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        files_by_week[week_start] = filename
        lap_counts_by_week[week_start] = lap_counts

    current_files = set(files_by_week.values())
    transition_files = existing_files_before if not manifest_was_valid else set()
    retained_files = sorted(
        (
            {
                str(entry["file"])
                for entry in previous_weeks.values()
                if entry.get("file") not in current_files
            }
            | transition_files
        )
        - current_files
    )
    return {
        "files_by_week": files_by_week,
        "lap_counts_by_week": lap_counts_by_week,
        "manifest_path": manifest_path,
        "manifest": {
            "version": ACTIVITY_DETAILS_CACHE_VERSION,
            "weeks": current_weeks,
            "retained_files": retained_files,
        },
        "keep_files": current_files | set(retained_files),
        "rebuilt": rebuilt,
        "reused": reused,
    }


def _publish_activity_detail_manifest(
    activity_details_dir: Path,
    prepared: dict[str, Any],
) -> int:
    write_json(prepared["manifest_path"], prepared["manifest"])
    if not cross_process_locking_available():
        # Cross-process locking is unavailable; retaining old chunks is safer
        # than allowing another process to delete a newly published reference.
        return 0
    removed = 0
    keep_files = prepared["keep_files"]
    for stale_path in activity_details_dir.glob("*.js"):
        if stale_path.name in keep_files:
            continue
        stale_path.unlink()
        removed += 1
    return removed


def _apply_activity_detail_lap_counts(
    payload: dict[str, Any],
    lap_counts_by_week: dict[str, dict[str, list[int]]],
) -> None:
    days_by_date = {
        day["date"]: day
        for day in payload.get("days", [])
        if isinstance(day, dict) and day.get("date")
    }
    for lap_counts in lap_counts_by_week.values():
        for day_key, counts in lap_counts.items():
            day = days_by_date.get(day_key)
            if not day or not isinstance(counts, list):
                continue
            activities = day.get("activities")
            if not isinstance(activities, list):
                continue
            for activity, raw_count in zip(activities, counts, strict=False):
                if not isinstance(activity, dict):
                    continue
                count = _safe_int(raw_count) or 0
                activity["lap_count"] = count
                activity["lap_count_label"] = f"{count} laps" if count else None


def _activity_detail_fingerprints_match(
    data_dir: Path,
    activity_ids_by_week: dict[str, dict[str, list[str]]],
    activities_by_id: dict[str, dict[str, Any]],
    ride_annotations: dict[str, dict[str, Any]],
    prepared: dict[str, Any],
) -> bool:
    manifest_weeks = prepared.get("manifest", {}).get("weeks", {})
    for week_start, day_activity_ids in activity_ids_by_week.items():
        expected = manifest_weeks.get(week_start, {}).get("fingerprint")
        if not expected:
            return False
        current = _activity_detail_week_fingerprint(
            day_activity_ids,
            activities_by_id,
            ride_annotations,
            data_dir,
        )
        if current != expected:
            return False
    return True


def _athlete_header(payload: dict[str, Any]) -> tuple[str, str]:
    athlete = payload.get("athlete") or {}
    if not isinstance(athlete, dict):
        return "Athlete", "Profile setup incomplete"
    name = str(athlete.get("display_name") or "").strip() or "Athlete"
    disciplines = [
        str(value).strip()
        for value in (athlete.get("disciplines") or [])
        if str(value).strip()
    ]
    experience = str(athlete.get("experience_level") or "").strip()
    profile_complete = bool(
        athlete.get("timezone")
        and athlete.get("unit_system") in {"metric", "imperial"}
        and disciplines
        and (experience or athlete.get("experience_years") is not None)
        and athlete.get("weekly_availability")
    )
    descriptor = " / ".join(value for value in (", ".join(disciplines), experience) if value)
    if not profile_complete:
        descriptor = "Profile setup incomplete"
    elif not descriptor:
        descriptor = f"{athlete['unit_system']} rider"
    return name, descriptor


def _build_training_center_unlocked(data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    derived_dir = _ensure_private_output_directory(data_dir, Path("derived"))
    (
        payload,
        activity_ids_by_week,
        activities_by_id,
        ride_annotations,
    ) = _build_payload(data_dir)
    data_js = derived_dir / "training_center_data.js"
    activity_details_dir = derived_dir / "training_center_activity_details"
    html_path = derived_dir / "training_center.html"
    favicon_path = derived_dir / "training_center_favicon.svg"
    progress = build_progress_artifact(data_dir)
    rider_name, rider_description = _athlete_header(payload)
    prepared_details: dict[str, Any] | None = None
    for _attempt in range(3):
        candidate_details = _prepare_activity_detail_sidecars(
            data_dir,
            activity_details_dir,
            activity_ids_by_week,
            activities_by_id,
            ride_annotations,
        )
        if _activity_detail_fingerprints_match(
            data_dir,
            activity_ids_by_week,
            activities_by_id,
            ride_annotations,
            candidate_details,
        ):
            prepared_details = candidate_details
            break
    if prepared_details is None:
        raise RuntimeError(
            "Ride detail inputs changed repeatedly during refresh; retry."
        )
    _apply_activity_detail_lap_counts(
        payload,
        prepared_details["lap_counts_by_week"],
    )
    for week in payload["weeks"]:
        details_file = prepared_details["files_by_week"].get(week["start_date"])
        if details_file:
            week["activity_details_file"] = details_file
            week["activity_details_key"] = prepared_details["manifest"]["weeks"][
                week["start_date"]
            ]["fingerprint"]
    write_text(
        data_js,
        "window.__COACH_TRAINING_CENTER_DATA__ = "
        + json.dumps(payload, separators=(",", ":"))
        + ";\n",
    )
    data_version = (
        str(payload["generatedAt"])
        .replace("-", "")
        .replace(":", "")
        .replace("+", "")
    )
    write_text(
        html_path,
        HTML_TEMPLATE.replace(
            "__TRAINING_CENTER_DATA_SRC__",
            f"./training_center_data.js?v={data_version}",
        )
        .replace("__RIDER_NAME__", html_escape(rider_name))
        .replace("__RIDER_DESCRIPTION__", html_escape(rider_description)),
    )
    write_text(favicon_path, FAVICON_SVG)
    removed_detail_files = _publish_activity_detail_manifest(
        activity_details_dir,
        prepared_details,
    )
    return {
        "html": str(html_path),
        "data_js": str(data_js),
        "activity_details_dir": str(activity_details_dir),
        "activity_detail_files": len(activity_ids_by_week),
        "activity_detail_files_rebuilt": prepared_details["rebuilt"],
        "activity_detail_files_reused": prepared_details["reused"],
        "activity_detail_files_removed": removed_detail_files,
        "favicon": str(favicon_path),
        "progress_html": progress["html"],
        "progress_source": progress["source"],
        "progress_error": progress["error"],
        "weeks": len(payload["weeks"]),
        "days": len(payload["days"]),
        "seed_notes": len(payload["notes"]),
    }


def build_training_center(data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    with _training_center_build_lock(data_dir):
        ensure_text_line(data_dir / ".gitignore", ".codex/cache/")
        ensure_text_line(data_dir / ".gitignore", "derived/.cache/")
        return _build_training_center_unlocked(data_dir)
