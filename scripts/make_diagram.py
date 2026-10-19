#!/usr/bin/env python3
"""Regenerate the Chief architecture blueprint.

    python scripts/make_diagram.py            # writes docs/chief-architecture.excalidraw (+ the PNG if matplotlib is installed)
    python scripts/make_diagram.py --no-png   # JSON only

Standard library only for the .excalidraw file (open it at https://excalidraw.com). The optional PNG preview is
drawn with matplotlib *from the saved JSON*, so it shows what the file really contains; the same pass measures
every label and reports any that spill outside its box. Without matplotlib the PNG is skipped and the script says so.

Border style is part of the message:
    solid   runs in mock mode and is covered by tests
    dashed  code is written but has NOT been run against the real service
    dotted  being built by another session; may not be in the repo yet
Edit STATUS below when a piece changes state, then re-run.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "docs" / "chief-architecture.excalidraw"
OUT_PNG = ROOT / "docs" / "chief-architecture-preview.png"

W, H = 1680, 1096
CHAR_W = 0.60  # average glyph width / font size; deliberately wide so wrapped text always fits
LINE_H = 1.25

# ---- status of every box: solid | dashed | dotted ------------------------------------------------------------
STATUS = {
    "phone": "dashed", "stage": "solid", "windows": "dashed", "slack": "dashed",
    "intent": "solid", "convo": "solid",
    "heartbeat": "solid", "runner": "solid", "gate": "solid", "memory": "solid", "bus": "solid",
    "audit": "solid", "email_brief": "solid", "draft_reply": "solid", "meeting_prep": "solid",
    "trip_planner": "solid", "open_brief": "solid", "files": "solid", "general": "dashed",
    "contract": "solid", "scoped": "solid", "replay": "solid", "recording": "solid",
    "mock": "solid", "live": "dashed", "llmclient": "dashed", "localtool": "solid",
    "whisper": "dashed", "composio": "dashed", "parallel": "dashed", "slacksvc": "dashed", "llm": "dashed", "folders": "solid",
    "x_stt": "dashed", "x_cal": "dashed", "x_gmail": "dashed", "x_web": "dashed", "x_slack": "dashed", "x_model": "dashed", "x_files": "solid",
}

PALETTE = {  # (fill, stroke)
    "iface": ("#a5d8ff", "#1971c2"),
    "route": ("#d0bfff", "#6741d9"),
    "core": ("#e5dbff", "#6741d9"),
    "gate": ("#ffc9c9", "#e03131"),
    "skill": ("#b2f2bb", "#2f9e44"),
    "tool": ("#ffec99", "#f08c00"),
    "svc": ("#ffd8a8", "#e8590c"),
    "ext": ("#e9ecef", "#495057"),
    "note": ("#fff3bf", "#f08c00"),
}

rng = random.Random(42)
elements: list[dict] = []
_n = [0]


def _id(prefix: str) -> str:
    _n[0] += 1
    return f"{prefix}{_n[0]}"


def wrap(text: str, width: float, fs: float) -> list[str]:
    """Greedy word wrap using the conservative glyph width. Explicit newlines are kept."""
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if len(trial) * fs * CHAR_W <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _base(kind: str, x: float, y: float, w: float, h: float, **kw) -> dict:
    el = {
        "id": kw.pop("id", None) or _id(kind[:2]),
        "type": kind, "x": round(x, 1), "y": round(y, 1), "width": round(w, 1), "height": round(h, 1), "angle": 0,
        "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 2,
        "strokeStyle": "solid", "roughness": 0, "opacity": 100, "groupIds": [], "frameId": None, "roundness": None,
        "seed": rng.randint(1, 2**31 - 1), "version": 1, "versionNonce": rng.randint(1, 2**31 - 1), "isDeleted": False,
        "boundElements": [], "updated": 1, "link": None, "locked": False,
    }
    el.update(kw)
    return el


def text(x, y, s, fs=14, color="#1e1e1e", align="left", width=None, group=None) -> dict:
    lines = s.split("\n")
    w = width if width is not None else max(len(l) for l in lines) * fs * CHAR_W
    h = len(lines) * fs * LINE_H
    el = _base("text", x, y, w, h, strokeColor=color, text=s, originalText=s, fontSize=fs, fontFamily=2,
               textAlign=align, verticalAlign="top", containerId=None, lineHeight=LINE_H, autoResize=False)
    if group:
        el["groupIds"] = [group]
    elements.append(el)
    return el


def box(key, x, y, w, h, title, body, kind, *, title_fs=15, body_fs=11.5):
    """Rectangle + title + wrapped body, grouped. Raises if the text cannot fit: layout bugs fail loudly."""
    fill, stroke = PALETTE[kind]
    style = STATUS.get(key, "solid")
    g = _id("g")
    rect = _base("rectangle", x, y, w, h, id=key, backgroundColor=fill, strokeColor=stroke, strokeStyle=style,
                 roundness={"type": 3}, groupIds=[g])
    elements.append(rect)
    inner = w - 16
    tl = wrap(title, inner, title_fs)
    bl = wrap(body, inner, body_fs) if body else []
    th = len(tl) * title_fs * LINE_H
    bh = len(bl) * body_fs * LINE_H
    total = th + (5 if bl else 0) + bh
    if total > h - 10:
        raise ValueError(f"box '{key}' needs {total:.0f}px of text but is only {h}px tall: {title!r}")
    top = y + (h - total) / 2
    text(x + 8, top, "\n".join(tl), title_fs, "#1e1e1e", "center", inner, g)
    if bl:
        text(x + 8, top + th + 5, "\n".join(bl), body_fs, "#343a40", "center", inner, g)
    return rect


def frame(x, y, w, h, label, color):
    elements.append(_base("rectangle", x, y, w, h, strokeColor=color, strokeStyle="dashed", strokeWidth=1,
                          roundness={"type": 3}))
    text(x + 12, y + 5, label, 11, color)


def arrow(points, color="#495057", label=None, label_at=None, style="solid"):
    (x0, y0) = points[0]
    rel = [[round(px - x0, 1), round(py - y0, 1)] for px, py in points]
    w = max(abs(p[0]) for p in rel)
    h = max(abs(p[1]) for p in rel)
    elements.append(_base("arrow", x0, y0, w, h, strokeColor=color, strokeWidth=1.5, strokeStyle=style, points=rel,
                          lastCommittedPoint=None, startBinding=None, endBinding=None, startArrowhead=None,
                          endArrowhead="arrow", roundness=None))
    if label:
        lx, ly = label_at or (x0 + 4, y0 + 4)
        text(lx, ly, label, 10.5, color)


def layer_label(y, h, s):
    text(20, y + h / 2 - 7, s, 12, "#868e96")


# ---------------------------------------------------------------------------------------------------------------
def build() -> None:
    text(170, 16, "Chief: personal agent architecture", 30, "#1e1e1e")
    text(170, 56, "Open source  |  open models  |  every write waits for an Approve tap  |  solid = runs in mock mode, "
         "dashed = not yet run against the real service", 12.5, "#495057")

    # ---- interfaces --------------------------------------------------------------------------------------
    y = 104
    layer_label(y, 84, "INTERFACES")
    box("phone", 170, y, 300, 84, "Phone voice page", "push-to-talk or typed, speaks replies, Approve / Skip buttons. HTTPS tunnel + token", "iface")
    box("slack", 600, y, 280, 84, "Slack", "proposal cards with Approve / Skip, Socket Mode, plain-code button handler", "iface")
    box("stage", 900, y, 230, 84, "Stage screen", "projector view: inbox, calendar, research, step log, caption", "iface")
    box("windows", 1150, y, 220, 84, "Real browser windows", "Gmail, Calendar and source pages open as Chief works", "iface")

    # ---- routing -----------------------------------------------------------------------------------------
    y = 226
    layer_label(y, 84, "ROUTING")
    box("intent", 170, y, 280, 84, "Intent mapper", "approve / skip words, then trigger phrases, then the model picks a skill", "route")
    box("convo", 490, y, 300, 84, "Conversation + parallel tasks", "follow-ups and new requests while earlier tasks keep running", "route")

    # ---- core --------------------------------------------------------------------------------------------
    cy = 346
    layer_label(cy, 128, "CORE")
    frame(170, cy, 1200, 128, "Chief core (Python)", "#6741d9")
    bw, gap, bx, by = 214.4, 24, 186, cy + 32
    core = [
        ("heartbeat", "Heartbeat", "every few hours, active hours only, silent unless something matters", "core"),
        ("runner", "Skill runner", "gives each skill its scoped read tools, collects the proposal", "core"),
        ("gate", "Approval gate", "every write waits for a tap; re-checks the skill's declared tools", "gate"),
        ("memory", "Memory", "decisions + activity log as plain files you can edit", "core"),
        ("bus", "ActivityBus", "every read, write and approval as one event stream", "core"),
    ]
    for i, (k, t, b, kind) in enumerate(core):
        box(k, bx + i * (bw + gap), by, bw, 84, t, b, kind)

    # ---- skills ------------------------------------------------------------------------------------------
    sy = 516
    layer_label(sy, 122, "SKILLS")
    frame(170, sy, 1120, 122, "Skills: one file each in chief/skills/, each defines SKILL = Skill(...) and run(ctx)", "#2f9e44")
    skills = [
        ("audit", "audit", "sleep vs meetings, proposes a lighter day"),
        ("email_brief", "email_brief", "read-only brief of recent mail"),
        ("draft_reply", "draft_reply", "drafts a reply, send waits for approval"),
        ("meeting_prep", "meeting_prep", "mail history + web background"),
        ("open_brief", "open_brief", "opens the one-page brief"),
        ("trip_planner", "trip_planner", "Caltrain times from pinned sources"),
        ("files", "files", "opens allow-listed folders"),
        ("general", "general", "any request, read tools only"),
    ]
    sw, sg = 128.25, 10
    for i, (k, t, b) in enumerate(skills):
        box(k, 182 + i * (sw + sg), sy + 24, sw, 84, t, b, "skill", title_fs=13, body_fs=10.5)

    # ---- tools -------------------------------------------------------------------------------------------
    ty = 672
    layer_label(ty, 90, "TOOLS")
    tw = [330, 250, 250, 330]
    tx = 170
    row = [
        ("contract", "Tools contract (tools/base.py)", "READ, SAFE WRITE (draft, Slack note), APPROVAL-ONLY (create / patch / delete event, send_email)"),
        ("scoped", "ScopedTools", "a skill sees only its declared read tools; write methods are hidden"),
        ("replay", "Replay + failover", "serves recorded results if a live call fails or Wi-Fi is off"),
        ("recording", "RecordingTools", "wraps every call and reports it to the ActivityBus"),
    ]
    xs = []
    for (k, t, b), w in zip(row, tw):
        xs.append(tx)
        box(k, tx, ty, w, 90, t, b, "tool")
        tx += w + (1200 - sum(tw)) / 3
    by2, bw2 = 776, 289.5
    row2 = [
        ("mock", "MockTools", "seeded calendar, mail and sleep data; no network"),
        ("live", "LiveTools", "Composio Calendar and Gmail, Parallel search, Slack post"),
        ("llmclient", "LLM client (llm.py)", "OpenAI-compatible: phrasing, intent pick, general loop"),
        ("localtool", "Local actions", "allow-listed folders only"),
    ]
    for i, (k, t, b) in enumerate(row2):
        box(k, 170 + i * (bw2 + 14), by2, bw2, 60, t, b, "tool", title_fs=14, body_fs=11)

    # ---- services ----------------------------------------------------------------------------------------
    vy = 876
    layer_label(vy, 84, "SERVICES")
    sw2 = 188.3
    svcs = [
        ("whisper", "Whisper STT", "large-v3-turbo on any OpenAI-compatible host (Groq default)"),
        ("composio", "Composio", "managed auth + Google Calendar and Gmail tools"),
        ("parallel", "Parallel", "web search, fast mode, pinned domains"),
        ("slacksvc", "Slack API", "Bolt Socket Mode, no public URL"),
        ("llm", "Open-source LLM", "any hosted or local open model, key + model in .env"),
        ("folders", "Local folders", "allow-listed paths, nothing else"),
    ]
    for i, (k, t, b) in enumerate(svcs):
        box(k, 170 + i * (sw2 + 14), vy, sw2, 84, t, b, "svc", title_fs=14, body_fs=11)

    # ---- external ----------------------------------------------------------------------------------------
    ey = 1000
    layer_label(ey, 62, "EXTERNAL")
    ext = [
        ("x_stt", 170, 140, "Speech-to-text host", "ext"),
        ("x_cal", 330, 140, "Google Calendar", "ext"),
        ("x_gmail", 480, 140, "Gmail", "ext"),
        ("x_web", 640, 190, "The live web", "ext"),
        ("x_slack", 850, 190, "Slack workspace", "ext"),
        ("x_model", 1050, 190, "Hosted open-model API", "ext"),
        ("x_files", 1250, 120, "Local files", "ext"),
    ]
    for k, x, w, t, kind in ext:
        box(k, x, ey, w, 62, t, "", kind, title_fs=13)

    # ---- arrows ------------------------------------------------------------------------------------------
    c = "#495057"
    arrow([(320, 188), (320, 226)], c, "text", (326, 198))
    arrow([(450, 268), (490, 268)], c)
    arrow([(540, 310), (540, 378)], c, "run a skill", (546, 330))
    arrow([(750, 310), (730, 378)], c, "approve / skip", (648, 322))
    arrow([(830, 188), (830, 378)], "#e03131", "tap (plain code, no LLM)", (836, 198))
    arrow([(1247, 378), (1247, 188)], "#6741d9", "events", (1253, 330))
    arrow([(1170, 378), (1040, 188)], "#6741d9", "events (polled)", (1100, 222))
    arrow([(398, 420), (424, 420)], c)
    arrow([(638.8, 420), (662.8, 420)], c)
    arrow([(877.2, 420), (901.2, 420)], c)
    arrow([(530, 462), (530, 516)], c, "loads", (536, 480))
    arrow([(640, 638), (640, 672)], c, "reads, scoped", (646, 646))
    arrow([(770, 462), (770, 494), (1340, 494), (1340, 672)], "#e03131", "approved writes only", (1100, 477))
    # tools layer -> services (LiveTools fans out)
    arrow([(560, 836), (466, 876)], c)
    arrow([(620, 836), (668, 876)], c)
    arrow([(690, 836), (871, 876)], c)
    arrow([(921, 836), (1073, 876)], c)
    arrow([(1225, 836), (1275, 876)], c)
    # services -> external
    arrow([(464, 960), (400, 1000)], c)
    arrow([(500, 960), (550, 1000)], c)
    arrow([(668, 960), (735, 1000)], c)
    arrow([(871, 960), (945, 1000)], c)
    arrow([(1073, 960), (1145, 1000)], c)
    arrow([(1275, 960), (1310, 1000)], c)
    # Whisper: left gutter up to the phone page, and down to the STT host
    arrow([(264, 960), (240, 1000)], c)
    arrow([(170, 918), (152, 918), (152, 146), (170, 146)], "#1971c2", "audio", (118, 492))

    # ---- notes column ------------------------------------------------------------------------------------
    nx, nw = 1420, 240
    box("note_rules", nx, 104, nw, 190, "Rules", "Reads run freely. Every write (calendar edit, send email) waits for an Approve tap. "
        "Email, web and calendar text is untrusted data. Skills get read tools only and can only propose tools they declared.", "note", body_fs=11.5)
    box("note_modes", nx, 310, nw, 200, "Modes (CHIEF_MODE)", "mock: seeded data, no network.\nlive: Composio + Parallel + Slack.\n"
        "demo: live first, recorded reads if a call fails or times out; writes are never faked.\n"
        "demo-offline: recorded reads, simulated writes (tagged).", "note", body_fs=11.5)
    box("note_loop", nx, 526, nw, 110, "The live loop", "speak, intent, skill, proposal, Approve, write; the Stage and the real windows follow the ActivityBus.", "note", body_fs=11.5)
    # legend
    text(nx, 658, "Legend", 13, "#495057")
    legend = [("solid", "runs in mock mode, tested"), ("dashed", "written, not yet run against the real service")]
    if "dotted" in STATUS.values():
        legend.append(("dotted", "being built, may not be in the repo yet"))
    for i, (style, label) in enumerate(legend):
        yy = 684 + i * 50
        elements.append(_base("rectangle", nx, yy, 44, 30, backgroundColor="#f1f3f5", strokeColor="#495057", strokeStyle=style, roundness={"type": 3}))
        text(nx + 54, yy + 2, "\n".join(wrap(label, 180, 11.5)), 11.5, "#343a40")


def save_json() -> None:
    build()
    doc = {"type": "excalidraw", "version": 2, "source": "scripts/make_diagram.py", "elements": elements,
           "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": {}}
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    json.loads(OUT_JSON.read_text(encoding="utf-8"))  # validate: the file must load back
    print(f"wrote {OUT_JSON.relative_to(ROOT)} ({len(elements)} elements)")


def render_png() -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch
    except ImportError:
        print("matplotlib is not installed: skipped docs/chief-architecture-preview.png (the .excalidraw file is complete).")
        return False

    doc = json.loads(OUT_JSON.read_text(encoding="utf-8"))
    els = doc["elements"]
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    ls = {"solid": "-", "dashed": (0, (5, 3)), "dotted": (0, (1, 2))}
    rects = []
    texts = []
    for e in els:
        if e["type"] == "rectangle":
            r = e["roundness"] is not None
            p = FancyBboxPatch((e["x"], e["y"]), e["width"], e["height"], boxstyle="round,pad=0,rounding_size=8" if r else "square,pad=0",
                               fc=e["backgroundColor"] if e["backgroundColor"] != "transparent" else "none", ec=e["strokeColor"],
                               lw=e["strokeWidth"], ls=ls[e["strokeStyle"]])
            ax.add_patch(p)
            rects.append(e)
        elif e["type"] == "arrow":
            pts = [(e["x"] + px, e["y"] + py) for px, py in e["points"]]
            xs, ys = zip(*pts)
            ax.plot(xs[:-1], ys[:-1], color=e["strokeColor"], lw=e["strokeWidth"], solid_capstyle="butt")
            ax.annotate("", xy=pts[-1], xytext=pts[-2], arrowprops=dict(arrowstyle="-|>", color=e["strokeColor"], lw=e["strokeWidth"], shrinkA=0, shrinkB=0))
            if len(pts) > 2:
                pass
        elif e["type"] == "text":
            ha = {"left": "left", "center": "center"}[e["textAlign"]]
            tx = e["x"] + (e["width"] / 2 if ha == "center" else 0)
            t = ax.text(tx, e["y"], e["text"], fontsize=e["fontSize"] * 0.72, color=e["strokeColor"], ha=ha, va="top",
                        linespacing=1.18, family="DejaVu Sans")
            texts.append((e, t))
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    problems = 0
    for e, t in texts:
        bb = t.get_window_extent(rend)
        (x0, y0), (x1, y1) = inv.transform((bb.x0, bb.y0)), inv.transform((bb.x1, bb.y1))
        lo_y, hi_y = min(y0, y1), max(y0, y1)
        # the box this text belongs to (same group), or the canvas for free text
        gid = e["groupIds"][0] if e["groupIds"] else None
        if gid:
            owner = next(r for r in rects if gid in r["groupIds"])
            if x0 < owner["x"] + 2 or x1 > owner["x"] + owner["width"] - 2 or lo_y < owner["y"] + 2 or hi_y > owner["y"] + owner["height"] - 2:
                problems += 1
                print(f"OVERFLOW in box '{owner['id']}': {e['text'][:50]!r}  text {x0:.0f}-{x1:.0f} x {lo_y:.0f}-{hi_y:.0f}  box {owner['x']:.0f}-{owner['x'] + owner['width']:.0f} x {owner['y']:.0f}-{owner['y'] + owner['height']:.0f}")
        elif x1 > W or hi_y > H:
            problems += 1
            print(f"OFF CANVAS: {e['text'][:50]!r}")
    fig.savefig(OUT_PNG, dpi=100, facecolor="white")
    plt.close(fig)
    print(f"wrote {OUT_PNG.relative_to(ROOT)}; label overflows found: {problems}")
    return problems == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-png", action="store_true", help="skip the matplotlib preview")
    args = ap.parse_args()
    save_json()
    if not args.no_png and not render_png():
        return 0  # a skipped or flagged preview is reported above; the .excalidraw file is still written
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
