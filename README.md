# Drawing-RNG Stroke Token Encoder v0.3

A small Flask + browser prototype for experimenting with **tolerant stroke-token graphical seeds**.

This is not a secure RNG and not a password manager. It is a research prototype for testing how human drawings can be converted into symbolic token sequences that are more redraw-tolerant than raw pixel hashing.

## What v0.3 adds over v0.2

- **Order mode**:
  - `drawn`: preserves the order in which strokes were drawn. Useful when the secret is a gesture.
  - `spatial`: sorts strokes top-to-bottom / left-to-right before tokenization. Useful when the secret is the final visual drawing and drawing order should not dominate similarity.
- **Turn direction fix**: v0.2 reversed left/right because the direction table is counter-clockwise.
- **Optional turn magnitude**: `TL_S`, `TL_M`, `TL_H`, `TR_S`, `TR_M`, `TR_H`.
- **RDP path simplification**: removes small hand wobble before resampling.
- **Ambiguity flags**: reports features near quantization boundaries, such as unstable directions, start zones, length buckets, or closed-shape thresholds.
- **Multi-view similarity**: compares overall tokens, direction tokens, structure tokens, pen-up movement, relation tokens, and turn tokens separately.
- **Profiles**: tolerant, balanced, and strict parameter presets.

## Why order mode matters

If the project is modeling a **gesture secret**, stroke order should matter. A house drawn before a lake is a different gesture from a lake drawn before a house.

If the project is modeling a **visual drawing secret**, stroke order should not dominate. In that case use `spatial` order mode. It canonicalizes strokes by position so two similar final drawings can produce more similar tokens even if the user drew the parts in a different order.

This is a research tradeoff: ignoring order improves redraw tolerance, but it also discards some secret information.

## Run

```bash
cd drawing_rng_stroke_v03
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Tokenization flow

```text
browser canvas strokes
→ Python backend
→ clean strokes
→ normalize position/scale
→ optional RDP simplification
→ optional spatial stroke ordering
→ resample at equal spacing
→ direction/turn/length tokens
→ structure/relation/pen-up tokens
→ serialized seed material
```

## Research use

Start by drawing the same visual concept 5 times under:

- tolerant profile
- balanced profile
- strict profile
- drawn order
- spatial order

Record which settings improve redraw similarity and which settings collapse too much detail.

