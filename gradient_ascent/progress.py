from __future__ import annotations

import importlib.util
import re
from html import escape
from pathlib import Path
from typing import Any, Callable


PROGRESS_HTML_FILENAME = "progress.html"
GOAL_MEASUREMENT_RELATIVE_PATH = Path("plan") / "goal_measurement.py"


def _goal_contract(data_dir: Path) -> tuple[str | None, list[dict[str, Any]], str | None]:
    path = data_dir / "plan" / "goals.md"
    if not path.exists():
        return None, [], None
    north_star: str | None = None
    goals: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section = ""
    evidence: str | None = None
    field_pattern = re.compile(r"^-\s*\*\*([^*]+?):\*\*\s*(.+)$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# ") and north_star is None:
            candidate = line[2:].strip()
            if "replace" not in candidate.lower():
                north_star = candidate
            continue
        if line.startswith("## "):
            section = line[3:].strip().lower()
            current = None
            continue
        if section == "main goals" and line.startswith("### "):
            title = line[4:].strip()
            current = None if "replace" in title.lower() else {"title": title, "fields": {}}
            if current is not None:
                goals.append(current)
            continue
        match = field_pattern.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if "replace" in value.lower():
            continue
        if section == "main goals" and current is not None:
            current["fields"][key] = value
        elif section == "measurement plan" and evidence is None and "evidence" in key:
            evidence = value
    return north_star, goals, evidence


def goals_progress_html(data_dir: Path) -> str:
    north_star, goals, measurement_evidence = _goal_contract(data_dir)
    if not north_star and not goals:
        body = """
        <section class="empty">
          <p class="eyebrow">Coaching contract</p>
          <h1>Goals not configured</h1>
          <p>Complete the goals step to define what training decisions should optimize for.</p>
        </section>"""
    else:
        cards: list[str] = []
        for goal in goals:
            fields = goal["fields"]
            details = []
            for key, label in (
                ("why it matters", "Why it matters"),
                ("why this matters", "Why it matters"),
                ("success means", "Success means"),
                ("coaching implication", "Coaching implication"),
            ):
                if fields.get(key) and not any(item.startswith(f"<dt>{label}") for item in details):
                    details.append(
                        f"<dt>{label}</dt><dd>{escape(str(fields[key]))}</dd>"
                    )
            evidence = measurement_evidence or fields.get("direct evidence")
            if evidence:
                details.append(f"<dt>Evidence to watch</dt><dd>{escape(str(evidence))}</dd>")
            cards.append(
                '<article class="goal-card">'
                '<span class="status">Insufficient evidence</span>'
                f"<h2>{escape(str(goal['title']))}</h2>"
                f"<dl>{''.join(details)}</dl>"
                "<p class=\"honesty\">No progress judgment is made until the listed evidence is available.</p>"
                "</article>"
            )
        body = (
            '<header><p class="eyebrow">North star</p>'
            f"<h1>{escape(north_star or 'Cycling goals')}</h1>"
            "<p>Progress stays unscored when the workspace lacks direct evidence.</p></header>"
            f"<div class=\"goal-grid\">{''.join(cards)}</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Coach Progress</title>
  <style>
    :root {{ color-scheme: light; --text:#222724; --muted:#687168; --line:#dfe6dc; --surface:#fff; --soft:#f3f7f1; --accent:#2f6f4e; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--text); font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:transparent; }}
    main {{ padding:18px; }}
    header,.empty,.goal-card {{ border:1px solid var(--line); border-radius:18px; background:var(--surface); padding:18px; }}
    header {{ margin-bottom:14px; }}
    h1,h2,p {{ margin-top:0; }}
    h1 {{ margin-bottom:8px; font-size:1.35rem; }}
    h2 {{ margin:10px 0 14px; font-size:1.05rem; }}
    p,dd {{ color:var(--muted); line-height:1.45; }}
    .eyebrow {{ margin-bottom:6px; color:var(--accent); font-size:.72rem; font-weight:750; letter-spacing:.08em; text-transform:uppercase; }}
    .goal-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }}
    .status {{ display:inline-block; border-radius:999px; background:var(--soft); color:var(--accent); padding:5px 9px; font-size:.75rem; font-weight:700; }}
    dl {{ display:grid; gap:5px 12px; grid-template-columns:max-content 1fr; margin:0; }}
    dt {{ font-size:.78rem; font-weight:700; }}
    dd {{ margin:0 0 8px; font-size:.88rem; }}
    .honesty {{ margin:8px 0 0; font-size:.82rem; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>
"""


def default_progress_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Coach Progress</title>
  <style>
    :root {
      color-scheme: light;
      --text: #222724;
      --muted: #70776f;
      --line: #e4e8e0;
      --surface: #ffffff;
      --accent: #5aa34f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: transparent;
    }
    main {
      min-height: 320px;
      padding: 18px;
    }
    section {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--surface);
      padding: 18px;
    }
    p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <main>
    <section>
      <p>No coach-generated progress view yet. Add <code>plan/goal_measurement.py</code> with a <code>build_progress_html(data_dir)</code> function to generate this page.</p>
    </section>
  </main>
</body>
</html>
"""


def error_progress_html(message: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Coach Progress</title>
  <style>
    :root {{
      color-scheme: light;
      --text: #222724;
      --muted: #70776f;
      --line: #e4e8e0;
      --surface: #ffffff;
      --danger: #a53d34;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: transparent;
    }}
    main {{
      min-height: 320px;
      padding: 18px;
    }}
    section {{
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--surface);
      padding: 18px;
    }}
    h1 {{
      margin: 0 0 10px;
      color: var(--danger);
      font-size: 1.1rem;
    }}
    p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    code {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>Progress view failed to render</h1>
      <p><code>plan/goal_measurement.py</code> raised: {escape(message)}</p>
    </section>
  </main>
</body>
</html>
"""


def _load_builder(script_path: Path) -> Callable[[Path], str] | None:
    if not script_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("coach_workspace_goal_measurement", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load progress generator: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    builder = getattr(module, "build_progress_html", None)
    if builder is None or not callable(builder):
        raise RuntimeError(
            f"{script_path} must define build_progress_html(data_dir: pathlib.Path) -> str"
        )
    return builder


def build_progress_artifact(data_dir: Path) -> dict[str, Any]:
    derived_dir = data_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    script_path = data_dir / GOAL_MEASUREMENT_RELATIVE_PATH
    output_path = derived_dir / PROGRESS_HTML_FILENAME
    try:
        builder = _load_builder(script_path)
        if builder is None:
            html = goals_progress_html(data_dir)
            source = "default"
            error = None
        else:
            html = builder(data_dir)
            if not isinstance(html, str) or not html.strip():
                raise RuntimeError(
                    f"{script_path} build_progress_html(data_dir) must return non-empty HTML"
                )
            source = str(script_path)
            error = None
    except Exception as exc:
        html = error_progress_html(str(exc))
        source = str(script_path)
        error = str(exc)
    output_path.write_text(html, encoding="utf-8")
    return {"html": str(output_path), "source": source, "error": error}
