#!/usr/bin/env python3
"""
Euclid Socratic page generator.

Commands:
  python generate_prop.py spec <n> <proof.txt>   # Claude drafts prop<n>_spec.yaml
  python generate_prop.py build <spec.yaml>       # renders static/i<n>.html from spec
"""

import sys, json, re
from pathlib import Path

# ── Step-type decision guide baked into the AI prompt ─────────────────────────

STEP_TYPE_GUIDE = """
STEP TYPES — choose based on what the student must actively do:

  yn    Student CONFIRMS a visible or already-established fact.
        Correct answer is almost always YES; NO shows a correction and retries.
        Use for: "Can you see X?", "Do these circles intersect?", "Is AB a radius?"
        Do NOT use yn when the student must recall or produce knowledge.

  open  Student must RECALL, DEFINE, NAME, or APPLY a result.
        Use for: naming a postulate/CN, defining equilateral, applying a prior
        proposition, stating what two quantities have in common.
        'criteria': plain-English description of a correct answer.
        'hint': one direct sentence, shown after 2 failed attempts.

  draw  A new construction element is REVEALED on the diagram.
        Student confirms the step is legitimate, then clicks "Yes / Draw".
        Every Post. 1/2/3 application that draws something gets a draw step.
        Must have a non-empty 'reveal' list of SVG element IDs.

  say   Tutor states an obvious consequence; auto-advances after 700 ms.
        Use SPARINGLY — prefer open with a low bar. Only for steps so
        mechanical that asking about them would feel patronizing.

Ordering rules:
  1. Start with 1–2 yn steps to orient the student (given objects + goal).
  2. open before each Postulate use (recall what the postulate allows).
  3. draw for each construction action (one draw per Postulate application).
  4. open for every deductive step (radii equal, CN applies, etc.).
  5. Close with open steps for QEF/QED and why.
"""

SPEC_PROMPT = """\
You are building a Socratic teaching script for Euclid's Elements.

Proposition: I.{n}
Enunciation: {enunciation}

Full proof text:
{proof_text}

SVG element IDs available in the diagram (use in 'reveal' fields):
{svg_ids}

{type_guide}

Output ONLY a YAML block (no markdown fences, no commentary) using this schema
for EVERY step — include all keys even if null:

steps:
  - type: yn          # or open / draw / say
    type_rationale: "one sentence explaining the type choice"
    q: "question text shown in chat"
    no: null          # yn only: correction text if student says No
    criteria: null    # open only: plain-English correctness criterion
    hint: null        # open only: direct one-sentence hint after 2 fails
    hint_linked_idx: null    # open only: int index of step whose proof text to highlight
    display_linked_idx: null # open only: override which proof line is highlighted
    text: null        # Euclid proof sentence for left panel, or null
    just: null        # justification label e.g. "Post. 1", or null
    side: null        # short right-panel construction note, or null
    reveal: []        # list of SVG IDs to reveal on this step
"""

# ── spec command ──────────────────────────────────────────────────────────────

def cmd_spec(n: int, proof_file: Path, svg_ids=None):
    import anthropic, yaml

    text = proof_file.read_text().strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    enunciation = lines[0]

    if not svg_ids:
        svg_ids_str = "(fill in your SVG element IDs — e.g. dc-ab, dc-tri, dc-ext, etc.)"
    else:
        svg_ids_str = ", ".join(svg_ids)

    prompt = SPEC_PROMPT.format(
        n=n,
        enunciation=enunciation,
        proof_text=text,
        svg_ids=svg_ids_str,
        type_guide=STEP_TYPE_GUIDE,
    )

    client = anthropic.Anthropic()
    print(f"Calling Claude to draft Prop {n} spec…", file=sys.stderr)
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```ya?ml\s*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    parsed = yaml.safe_load(raw)
    if "steps" not in parsed:
        sys.exit("ERROR: AI output missing 'steps' key. Raw output:\n" + raw)

    out = Path(f"prop{n}_spec.yaml")
    header = (
        f"number: {n}\n"
        f"title: \"Euclid I.{n}\"\n"
        f"enunciation: \"{enunciation}\"\n"
        f"conclusion: \"Congratulations! You have completed Euclid's Elements Book I, Proposition {n}.\"\n"
        f"viewbox: \"0 -20 800 400\"\n"
        f"initial_visible: []\n"
        f"svg_canvas: |\n"
        f"  <!-- TODO: paste your SVG canvas XML here -->\n\n"
    )
    out.write_text(header + raw)
    print(f"Wrote {out}")
    print(f"Edit {out}: add svg_canvas + initial_visible, then run:")
    print(f"  python generate_prop.py build {out}")

# ── build command ─────────────────────────────────────────────────────────────

def cmd_build(spec_file: Path):
    import yaml
    from jinja2 import Environment, FileSystemLoader

    spec = yaml.safe_load(spec_file.read_text())
    n = spec["number"]

    # Strip type_rationale — it's metadata, not engine data
    # Also fix YAML boolean coercion: `no:` parses as False key
    steps = []
    for s in spec.get("steps", []):
        step = {("no" if k is False else k): v
                for k, v in s.items() if k != "type_rationale"}
        step.setdefault("reveal", [])
        step.setdefault("criteria", None)
        step.setdefault("hint", None)
        step.setdefault("hint_linked_idx", None)
        step.setdefault("display_linked_idx", None)
        step.setdefault("text", None)
        step.setdefault("just", None)
        step.setdefault("side", None)
        step.setdefault("no", None)
        steps.append(step)

    initial_visible = spec.get("initial_visible", [])

    env = Environment(loader=FileSystemLoader("templates"), autoescape=False)
    tmpl = env.get_template("prop_template.html")
    html = tmpl.render(
        title=spec.get("title", f"Euclid I.{n}"),
        h1=spec.get("title", f"Euclid I.{n}"),
        prop_label=f"I.{n}",
        prop_enunc=spec.get("enunciation", ""),
        viewbox=spec.get("viewbox", "0 -20 800 400"),
        svg_canvas=spec.get("svg_canvas", "<!-- SVG CANVAS -->").strip(),
        initial_visible_json=json.dumps(initial_visible),
        steps_json=json.dumps(steps, ensure_ascii=False),
        prop_num=n,
        conclusion_json=json.dumps(
            spec.get("conclusion", f"You completed Proposition {n}.")
        ),
    )

    out = Path(f"static/i{n}.html")
    out.write_text(html)
    print(f"Wrote {out}")
    print(f"Make sure server.py has an /i{n} route.")

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "spec":
        if len(args) < 3:
            sys.exit("Usage: python generate_prop.py spec <n> <proof.txt>")
        svg_ids = args[3].split(",") if len(args) > 3 else None
        cmd_spec(int(args[1]), Path(args[2]), svg_ids)
    elif cmd == "build":
        if len(args) < 2:
            sys.exit("Usage: python generate_prop.py build <spec.yaml>")
        cmd_build(Path(args[1]))
    else:
        sys.exit(f"Unknown command: {cmd}")

if __name__ == "__main__":
    main()
