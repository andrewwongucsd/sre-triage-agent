"""Render a terminal-style demo GIF for the README.

Produces docs/demo.gif: a dark terminal card that types the two headline
commands and reveals their real output — the mock demo diagnosis and the
`make eval` results table ending in GATE PASSED. Deterministic; regenerate
with `make gif`.

The text shown is copied from actual runs (see the SCENES list); this script
lays it out, it does not fabricate results.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.gif"
FONT = "/System/Library/Fonts/Menlo.ttc"

# Terminal palette (a calm, professional dark theme).
BG = (13, 17, 23)          # GitHub-dark canvas
CHROME = (22, 27, 34)
FG = (201, 209, 217)
DIM = (110, 118, 129)
PROMPT = (63, 185, 80)      # green $
CYAN = (57, 197, 187)
YELLOW = (210, 168, 76)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
DOTS = [(255, 95, 86), (255, 189, 46), (39, 201, 63)]

FS = 24                     # font size (rendered at 2x, downscaled for crispness)
PAD = 28
LINE = 32
COLS = 82

# (text, color, is_prompt_command). A command line animates char-by-char;
# output lines appear whole. None = blank line.
def cmd(t): return (t, FG, True)
def out(t, c=FG): return (t, c, False)

SCENES = [
    cmd("make demo"),
    out("Incident: checkout-db-pool  (clear)", DIM),
    out('{'),
    out('  "root_cause":  "postgres-primary pool degrading checkout-api",', FG),
    out('  "evidence":    ["ERROR checkout-api: could not get connection', DIM),
    out('                   from pool; postgres-primary timeout 5000ms", ...],', DIM),
    out('  "escalate_to": "database-platform",', CYAN),
    out('  "confidence":  0.8', YELLOW),
    out('}'),
    (None, None, False),
    cmd("make eval        # offline, no API key, gated in CI"),
    out("## Eval results", FG),
    out("Escalation accuracy (headline): 53.5% over 43 cases", FG),
    out("| band            | baseline |", DIM),
    out("| clear / ambiguous / no_data   |  100 / 50 / 100 % |", FG),
    out("| cascading / partial / conflict|    0 /  0 /  17 % |", FG),
    out("| false NO_DATA rate (guardrail)|        0%        |", GREEN),
    (None, None, False),
    out("✓  GATE PASSED: escalation accuracy 53.5% >= threshold 50.0%", GREEN),
]


def _load(scale: int):
    return (
        ImageFont.truetype(FONT, FS * scale),
        ImageFont.truetype(FONT, FS * scale, index=1),  # bold face in Menlo.ttc
    )


def render():
    scale = 2
    reg, bold = _load(scale)
    w = (PAD * 2 + COLS * (FS * scale) * 0.62)
    h = PAD * 2 + (LINE * scale) * (len(SCENES) + 2)
    W, H = int(w), int(h)

    def base():
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, PAD * scale + 6], fill=CHROME)  # title bar
        for i, c in enumerate(DOTS):
            d.ellipse([PAD * scale + i * 34 - 8, 18 * scale - 8,
                       PAD * scale + i * 34 + 8, 18 * scale + 8], fill=c)
        d.text((W // 2 - 150, 12 * scale), "sre-triage-agent — make", font=reg, fill=DIM)
        return img, d

    def draw_lines(d, lines_upto, typing_frac=1.0):
        y = PAD * scale + LINE * scale
        for idx, (text, color, is_cmd) in enumerate(SCENES[:lines_upto + 1]):
            x = PAD * scale
            last = idx == lines_upto
            if text is None:
                y += LINE * scale
                continue
            shown = text
            if is_cmd:
                d.text((x, y), "$", font=bold, fill=PROMPT)
                x += int(FS * scale * 0.62 * 2)
                if last and typing_frac < 1.0:
                    shown = text[: max(1, int(len(text) * typing_frac))]
            d.text((x, y), shown, font=(bold if is_cmd else reg),
                   fill=(FG if is_cmd else color))
            y += LINE * scale

    frames, durations = [], []
    # Reveal scene by scene; type out command lines char-by-char.
    for i, (text, _, is_cmd) in enumerate(SCENES):
        if is_cmd and text is not None:
            for f in (0.35, 0.7, 1.0):
                img, d = base()
                draw_lines(d, i, typing_frac=f)
                frames.append(img); durations.append(90)
        else:
            img, d = base()
            draw_lines(d, i)
            frames.append(img); durations.append(140 if text else 60)
    # Hold the final frame.
    img, d = base(); draw_lines(d, len(SCENES) - 1)
    frames.append(img); durations.append(2600)

    small = [f.resize((W // scale, H // scale), Image.LANCZOS) for f in frames]
    OUT.parent.mkdir(exist_ok=True)
    small[0].save(OUT, save_all=True, append_images=small[1:],
                  duration=durations, loop=0, optimize=True)
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(small)} frames, {kb:.0f} KB, {small[0].size})")


if __name__ == "__main__":
    render()
