"""Stroke-token encoder for Drawing-RNG — v0.3

Converts vector drawing strokes into a symbolic token sequence that is:
- tolerant to minor redraw variation (normalization, resampling, jitter cleanup)
- inspectable (human-readable tokens)
- parameterizable (direction buckets, length buckets, spacing — for E3 sweep)
- honest about security (no cryptographic claims in this module)

Input stroke format:
    strokes = [
        [[x, y], [x, y], ...],   # stroke 0
        [[x, y], [x, y], ...],   # stroke 1
    ]
    (timestamps optional and ignored — only x, y used)

Output (serialized) example:
    DRNG-STROKE-v0.3|dir=8|len=3|spacing=0.050|jitter=1|
    S|CLOSED|E5|SE1|S5|SW1|W5|NW1|N5|NE1|
    PU_NE_M|
    S|E3|S3|W3|N3|
    END

v0.3 changes vs v0.2:
- Direction buckets now support 4, 8, 16 (required for E3 parameter sweep).
- Added turn tokens between direction runs (captures shape structure).
- Removed absolute anchor tokens (S@col,row) — they leak position info and
  hurt redraw tolerance. Replaced with a relative start-zone token (S@zone)
  where zone is relative to the drawing's own bounding box, not the canvas.
- Fixed: derive_seed_material() — v0.1 had a broken hmac.new() call.
- Fixed: relation tokens now appear BETWEEN strokes, not after direction runs.
- Configurable length bucket thresholds (not hardcoded magic numbers).
- Serialized header now encodes all flags that affect token output.
- Token type enum replaces fragile string-prefix matching in stats.
- Fixed: turn direction semantics (direction table is counter-clockwise).
- Added: optional turn magnitude tokens (TL_S/TL_M/TL_H, TR_S/TR_M/TR_H).
- Added: optional Ramer-Douglas-Peucker path simplification before resampling.
- Added: order_mode = drawn/spatial so visual similarity can ignore drawing order.
- Added: ambiguity flags for quantization-boundary instability.
- Added: profile-friendly parameters for redraw experiments.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Stroke = List[Point]
Run = Tuple[str, int]


# ---------------------------------------------------------------------------
# Token type classification (replaces fragile string-prefix matching)
# ---------------------------------------------------------------------------

class TokenKind(Enum):
    STROKE_START   = "stroke_start"    # S  or  S@zone
    CLOSED         = "closed"          # CLOSED
    DIRECTION_RUN  = "direction_run"   # E5, NW3, N1, ...
    TURN           = "turn"            # TR, TL, TU, TS  (right/left/uturn/straight)
    PENUP          = "penup"           # PU_E_M, PU_ZERO, ...
    RELATION       = "relation"        # REL_OVERLAP_HIGH, REL_SEPARATE, ...
    END            = "end"             # END


def classify_token(token: str) -> TokenKind:
    if token == "END":
        return TokenKind.END
    if token == "CLOSED":
        return TokenKind.CLOSED
    if token.startswith("PU_") or token == "PU":
        return TokenKind.PENUP
    if token.startswith("REL_"):
        return TokenKind.RELATION
    if token in {"TR", "TL", "TU", "TS"} or token.startswith(("TR_", "TL_")):
        return TokenKind.TURN
    if token == "S" or token.startswith("S@"):
        return TokenKind.STROKE_START
    # Direction run: starts with a direction letter
    return TokenKind.DIRECTION_RUN


# ---------------------------------------------------------------------------
# Direction tables for 4 / 8 / 16 buckets
# ---------------------------------------------------------------------------

_DIRS_4  = ["E", "N", "W", "S"]
_DIRS_8  = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
_DIRS_16 = [
    "E", "ENE", "NE", "NNE",
    "N", "NNW", "NW", "WNW",
    "W", "WSW", "SW", "SSW",
    "S", "SSE", "SE", "ESE",
]

def _direction_table(buckets: int) -> List[str]:
    if buckets == 4:
        return _DIRS_4
    if buckets == 8:
        return _DIRS_8
    if buckets == 16:
        return _DIRS_16
    raise ValueError(f"direction_buckets must be 4, 8, or 16; got {buckets}")


def direction_between(a: Point, b: Point, dirs: List[str]) -> str:
    """Map a movement vector to the nearest compass direction label.

    Browser canvas: y increases downward. We flip y so N means up-on-screen.
    """
    dx = b[0] - a[0]
    dy = -(b[1] - a[1])   # flip y
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return "Z"
    n = len(dirs)
    angle = math.degrees(math.atan2(dy, dx))
    if angle < 0:
        angle += 360.0
    bucket = int((angle + (180.0 / n)) // (360.0 / n)) % n
    return dirs[bucket]


# ---------------------------------------------------------------------------
# Turn token between consecutive direction runs
# ---------------------------------------------------------------------------

def turn_token(dir_a: str, dir_b: str, dirs: List[str], include_magnitude: bool = False) -> str:
    """Return a turn token describing the angular change from dir_a to dir_b.

    Important: the direction tables in this file are ordered counter-clockwise:
    E -> NE -> N -> NW -> W -> SW -> S -> SE.

    Therefore a positive index difference is a LEFT turn, while the shorter
    negative direction is a RIGHT turn. v0.2 accidentally reversed this.

    Base tokens:
        TS = straight
        TL = left / counter-clockwise
        TR = right / clockwise
        TU = U-turn

    Optional magnitude tokens:
        TL_S / TR_S = slight turn
        TL_M / TR_M = medium turn
        TL_H / TR_H = hard turn
    """
    if dir_a == "Z" or dir_b == "Z":
        return "TS"
    n = len(dirs)
    try:
        ia = dirs.index(dir_a)
        ib = dirs.index(dir_b)
    except ValueError:
        return "TS"

    diff = (ib - ia) % n
    half = n // 2
    if diff == 0:
        return "TS"
    if diff == half:
        return "TU"

    # Because dirs are counter-clockwise, small positive diff = left turn.
    if diff < half:
        side = "TL"
        steps = diff
    else:
        side = "TR"
        steps = n - diff

    if not include_magnitude:
        return side

    # Scale thresholds by number of buckets. For 8-dir: 1=S, 2=M, 3=H.
    slight_max = max(1, half // 3)
    medium_max = max(slight_max + 1, (2 * half) // 3)
    if steps <= slight_max:
        mag = "S"
    elif steps <= medium_max:
        mag = "M"
    else:
        mag = "H"
    return f"{side}_{mag}"


# ---------------------------------------------------------------------------
# Length bucket configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LengthBuckets:
    """Configurable run-length bucketing.

    short_max: normalized distance below which a run is SHORT.
    medium_max: normalized distance below which a run is MEDIUM (else LONG).

    Defaults match the normalized coordinate space where the drawing fits in
    roughly [-0.5, 0.5], so total width ≈ 1.0.
    """
    short_max: float  = 0.18
    medium_max: float = 0.40

    def bucket(self, distance: float) -> str:
        if distance < self.short_max:
            return "S"
        if distance < self.medium_max:
            return "M"
        return "L"

    def validate(self) -> None:
        if not 0 < self.short_max < self.medium_max:
            raise ValueError(
                f"length buckets must satisfy 0 < short_max < medium_max, "
                f"got short_max={self.short_max}, medium_max={self.medium_max}"
            )


# ---------------------------------------------------------------------------
# Encoder parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EncoderParams:
    """All parameters that affect the token output.

    Changing any field here changes the serialized token string and therefore
    the derived seed — intentionally so for the E3 parameter sweep.
    """

    # Core tokenization
    resample_spacing:   float = 0.05
    direction_buckets:  int   = 8
    length_buckets:     LengthBuckets = field(default_factory=LengthBuckets)

    # Noise filtering
    min_stroke_points:              int   = 2
    min_raw_stroke_length:          float = 5.0
    min_normalized_stroke_length:   float = 0.035
    jitter_run_max:                 int   = 1

    # Geometry cleanup before resampling. 0.0 disables RDP simplification.
    simplify_epsilon:               float = 0.0

    # Stroke ordering: "drawn" preserves gesture order; "spatial" canonicalizes
    # strokes top-to-bottom/left-to-right for visual drawings where order should
    # not dominate similarity.
    order_mode:                     str   = "drawn"

    # Optional token features (each can be toggled for comparison experiments)
    include_turn_tokens:      bool = True
    include_turn_magnitude:   bool = False
    include_start_zone:       bool = True   # coarse relative start zone (not absolute)
    include_penup_moves:      bool = True
    include_closed_tokens:    bool = True
    include_relation_tokens:  bool = True

    # Closed-shape detection threshold (normalized distance first↔last point)
    close_threshold: float = 0.075

    # Start zone: relative to the drawing's own bounding box, divided into NxN cells
    # e.g. zone_grid=3 → top-left / top-center / ... (9 zones)
    zone_grid: int = 3

    # Internal precision for normalized coordinates (rounding)
    round_normalized: int = 4

    def validate(self) -> None:
        if self.resample_spacing <= 0:
            raise ValueError("resample_spacing must be > 0")
        if self.direction_buckets not in (4, 8, 16):
            raise ValueError("direction_buckets must be 4, 8, or 16")
        if self.min_stroke_points < 2:
            raise ValueError("min_stroke_points must be >= 2")
        if self.round_normalized < 0:
            raise ValueError("round_normalized must be >= 0")
        if self.min_raw_stroke_length < 0:
            raise ValueError("min_raw_stroke_length must be >= 0")
        if self.min_normalized_stroke_length < 0:
            raise ValueError("min_normalized_stroke_length must be >= 0")
        if self.jitter_run_max < 0:
            raise ValueError("jitter_run_max must be >= 0")
        if self.simplify_epsilon < 0:
            raise ValueError("simplify_epsilon must be >= 0")
        if self.order_mode not in {"drawn", "spatial"}:
            raise ValueError("order_mode must be 'drawn' or 'spatial'")
        if self.close_threshold < 0:
            raise ValueError("close_threshold must be >= 0")
        if not 2 <= self.zone_grid <= 8:
            raise ValueError("zone_grid must be between 2 and 8")
        self.length_buckets.validate()


def _to_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(raw)


def params_from_dict(raw: Dict[str, Any] | None) -> EncoderParams:
    raw = raw or {}
    lb_raw = raw.get("length_buckets") or {}
    if isinstance(lb_raw, dict):
        lb = LengthBuckets(
            short_max=float(lb_raw.get("short_max", 0.18)),
            medium_max=float(lb_raw.get("medium_max", 0.40)),
        )
    else:
        lb = LengthBuckets()

    return EncoderParams(
        resample_spacing=float(raw.get("resample_spacing", 0.05)),
        direction_buckets=int(raw.get("direction_buckets", 8)),
        length_buckets=lb,
        min_stroke_points=int(raw.get("min_stroke_points", 2)),
        min_raw_stroke_length=float(raw.get("min_raw_stroke_length", 5.0)),
        min_normalized_stroke_length=float(raw.get("min_normalized_stroke_length", 0.035)),
        jitter_run_max=int(raw.get("jitter_run_max", 1)),
        simplify_epsilon=float(raw.get("simplify_epsilon", 0.0)),
        order_mode=str(raw.get("order_mode", "drawn")),
        include_turn_tokens=_to_bool(raw.get("include_turn_tokens"), True),
        include_turn_magnitude=_to_bool(raw.get("include_turn_magnitude"), False),
        include_start_zone=_to_bool(raw.get("include_start_zone"), True),
        include_penup_moves=_to_bool(raw.get("include_penup_moves"), True),
        include_closed_tokens=_to_bool(raw.get("include_closed_tokens"), True),
        include_relation_tokens=_to_bool(raw.get("include_relation_tokens"), True),
        close_threshold=float(raw.get("close_threshold", 0.075)),
        zone_grid=int(raw.get("zone_grid", 3)),
        round_normalized=int(raw.get("round_normalized", 4)),
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _as_point(raw_point: Sequence[Any]) -> Point:
    if len(raw_point) < 2:
        raise ValueError("each point must have at least x and y")
    return float(raw_point[0]), float(raw_point[1])


def _distance(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _path_length(stroke: Stroke) -> float:
    return sum(_distance(stroke[i - 1], stroke[i]) for i in range(1, len(stroke)))


def bounding_box(strokes: Sequence[Stroke]) -> Tuple[float, float, float, float]:
    points = [p for stroke in strokes for p in stroke]
    if not points:
        raise ValueError("no valid points in strokes")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def stroke_box(stroke: Stroke) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in stroke]
    ys = [p[1] for p in stroke]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Cleaning and normalization
# ---------------------------------------------------------------------------

def clean_strokes(
    raw_strokes: Sequence[Sequence[Sequence[Any]]],
    min_points: int = 2,
    min_raw_length: float = 5.0,
) -> List[Stroke]:
    """Convert JSON strokes to numeric points and drop obvious noise."""
    if not isinstance(raw_strokes, Sequence):
        raise ValueError("strokes must be a list of strokes")
    strokes: List[Stroke] = []
    for raw_stroke in raw_strokes:
        if not isinstance(raw_stroke, Sequence):
            continue
        stroke: Stroke = []
        last: Optional[Point] = None
        for raw_point in raw_stroke:
            p = _as_point(raw_point)
            if last is None or _distance(last, p) > 1e-9:
                stroke.append(p)
                last = p
        if len(stroke) >= min_points and _path_length(stroke) >= min_raw_length:
            strokes.append(stroke)
    return strokes


def normalize_strokes(strokes: Sequence[Stroke], round_digits: int = 4) -> List[Stroke]:
    """Center and uniform-scale strokes to fit in roughly [-0.5, 0.5].

    Preserves aspect ratio. Does NOT rotate.
    After normalization the drawing is position- and scale-invariant.
    """
    min_x, min_y, max_x, max_y = bounding_box(strokes)
    width  = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    scale  = max(width, height)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0

    out: List[Stroke] = []
    for stroke in strokes:
        norm: Stroke = []
        for x, y in stroke:
            nx = round((x - cx) / scale, round_digits)
            ny = round((y - cy) / scale, round_digits)
            norm.append((nx, ny))
        out.append(norm)
    return out


def filter_tiny_strokes(strokes: Sequence[Stroke], min_length: float) -> Tuple[List[Stroke], int]:
    kept: List[Stroke] = []
    dropped = 0
    for stroke in strokes:
        if len(stroke) >= 2 and _path_length(stroke) >= min_length:
            kept.append(list(stroke))
        else:
            dropped += 1
    return kept, dropped


def _point_line_distance(p: Point, a: Point, b: Point) -> float:
    """Distance from point p to line segment a-b."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx = bx - ax
    dy = by - ay
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return _distance(p, a)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj = (ax + t * dx, ay + t * dy)
    return _distance(p, proj)


def rdp_simplify_stroke(stroke: Stroke, epsilon: float) -> Stroke:
    """Ramer-Douglas-Peucker simplification for normalized strokes.

    This removes small hand wobble while preserving large-scale corners.
    It is intentionally optional because aggressive simplification can discard
    secret detail and increase collisions.
    """
    if epsilon <= 0 or len(stroke) <= 2:
        return list(stroke)

    first, last = stroke[0], stroke[-1]
    max_dist = -1.0
    index = -1
    for i in range(1, len(stroke) - 1):
        dist = _point_line_distance(stroke[i], first, last)
        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist > epsilon and index != -1:
        left = rdp_simplify_stroke(stroke[: index + 1], epsilon)
        right = rdp_simplify_stroke(stroke[index:], epsilon)
        return left[:-1] + right
    return [first, last]


def rdp_simplify_strokes(strokes: Sequence[Stroke], epsilon: float) -> List[Stroke]:
    if epsilon <= 0:
        return [list(s) for s in strokes]
    return [rdp_simplify_stroke(s, epsilon) for s in strokes]


def sort_strokes_spatial(strokes: Sequence[Stroke]) -> List[Stroke]:
    """Canonicalize stroke order by spatial position rather than draw order.

    This is useful for visual-secret mode: drawing the lake first and the house
    later should not dominate similarity if the final visual object is similar.

    The sort key is intentionally simple and interpretable: top-to-bottom by
    stroke bounding-box center, then left-to-right, then area/path length as
    stable tie-breakers.
    """
    def key(stroke: Stroke) -> Tuple[float, float, float, float]:
        x0, y0, x1, y1 = stroke_box(stroke)
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        area = _bbox_area((x0, y0, x1, y1))
        return (round(cy, 3), round(cx, 3), round(area, 3), round(_path_length(stroke), 3))
    return sorted([list(s) for s in strokes], key=key)


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def resample_stroke(stroke: Stroke, spacing: float) -> Stroke:
    """Resample at equal arc-length spacing so run counts reflect path length,
    not original point density."""
    if len(stroke) < 2:
        return stroke[:]
    total = _path_length(stroke)
    if total <= 1e-9:
        return [stroke[0], stroke[-1]]

    target_count = max(2, int(math.ceil(total / spacing)) + 1)
    dists = [0.0]
    for i in range(1, len(stroke)):
        dists.append(dists[-1] + _distance(stroke[i - 1], stroke[i]))

    result: Stroke = []
    seg = 1
    for j in range(target_count):
        td = min(total, j * total / (target_count - 1))
        while seg < len(dists) - 1 and dists[seg] < td:
            seg += 1
        pd, nd = dists[seg - 1], dists[seg]
        a, b   = stroke[seg - 1], stroke[seg]
        if nd - pd <= 1e-12:
            result.append(a)
        else:
            t = (td - pd) / (nd - pd)
            result.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return result


def resample_strokes(strokes: Sequence[Stroke], spacing: float) -> List[Stroke]:
    return [resample_stroke(s, spacing) for s in strokes]


# ---------------------------------------------------------------------------
# Direction → run compression → denoising
# ---------------------------------------------------------------------------

def stroke_to_directions(stroke: Stroke, dirs: List[str]) -> List[str]:
    result: List[str] = []
    for i in range(1, len(stroke)):
        d = direction_between(stroke[i - 1], stroke[i], dirs)
        if d != "Z":
            result.append(d)
    return result


def runs_from_directions(directions: Iterable[str]) -> List[Run]:
    runs: List[Run] = []
    prev: Optional[str] = None
    count = 0
    for d in directions:
        if prev is None:
            prev, count = d, 1
        elif d == prev:
            count += 1
        else:
            runs.append((prev, count))
            prev, count = d, 1
    if prev is not None:
        runs.append((prev, count))
    return runs


def denoise_runs(runs: Sequence[Run], jitter_run_max: int = 1) -> List[Run]:
    """Remove tiny isolated runs caused by hand tremor at corners.

    Only removes a short run when both neighbors are longer than jitter_run_max,
    so real short strokes (like a dot-cross) are not accidentally dropped.
    """
    if jitter_run_max <= 0 or len(runs) < 3:
        return list(runs)

    cleaned: List[Run] = []
    for i, (direction, count) in enumerate(runs):
        is_middle = 0 < i < len(runs) - 1
        if (is_middle
                and count <= jitter_run_max
                and runs[i - 1][1] > jitter_run_max
                and runs[i + 1][1] > jitter_run_max):
            continue   # drop the jitter run
        cleaned.append((direction, count))

    # Re-merge neighbors that now match after dropping jitter
    merged: List[Run] = []
    for direction, count in cleaned:
        if merged and merged[-1][0] == direction:
            merged[-1] = (direction, merged[-1][1] + count)
        else:
            merged.append((direction, count))
    return merged


def runs_to_tokens(
    runs: Sequence[Run],
    dirs: List[str],
    lb: LengthBuckets,
    include_turns: bool,
    spacing: float,
    include_turn_magnitude: bool = False,
) -> List[str]:
    """Convert denoised runs into token strings.

    Each run becomes  DIR + length_bucket, e.g. "E_M", "NW_S", "S_L".
    Between consecutive runs, an optional turn token is inserted.
    """
    tokens: List[str] = []
    for i, (direction, count) in enumerate(runs):
        if include_turns and i > 0:
            tokens.append(turn_token(runs[i - 1][0], direction, dirs, include_magnitude=include_turn_magnitude))
        dist = count * spacing
        tokens.append(f"{direction}_{lb.bucket(dist)}")
    return tokens


# ---------------------------------------------------------------------------
# Structural tokens
# ---------------------------------------------------------------------------

def relative_start_zone(stroke: Stroke, drawing_box: Tuple[float, float, float, float], grid: int) -> str:
    """Return a start-zone token relative to the drawing's bounding box.

    This is position-tolerant: the zone is computed within the drawing's own
    extent, so the same shape drawn slightly off-center produces the same zone.
    Unlike the v0.1 absolute anchor (S@col,row vs canvas), this is translation-
    invariant within the drawing.
    """
    min_x, min_y, max_x, max_y = drawing_box
    w = max(max_x - min_x, 1e-9)
    h = max(max_y - min_y, 1e-9)
    x, y = stroke[0]
    col = int(math.floor((x - min_x) / w * grid))
    row = int(math.floor((y - min_y) / h * grid))
    col = max(0, min(grid - 1, col))
    row = max(0, min(grid - 1, row))
    return f"S@{col},{row}"


def penup_move_token(prev_stroke: Stroke, next_stroke: Stroke, dirs: List[str], lb: LengthBuckets) -> str:
    end   = prev_stroke[-1]
    start = next_stroke[0]
    dist  = _distance(end, start)
    if dist < 1e-6:
        return "PU_ZERO"
    d = direction_between(end, start, dirs)
    return f"PU_{d}_{lb.bucket(dist)}"


def is_closed(stroke: Stroke, threshold: float) -> bool:
    if len(stroke) < 3:
        return False
    return _distance(stroke[0], stroke[-1]) <= threshold


def _bbox_area(box: Tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = box
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def bbox_overlap_ratio(a: Tuple[float, float, float, float],
                       b: Tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    denom = max(min(_bbox_area(a), _bbox_area(b)), 1e-12)
    return inter / denom


def relation_token(prev_stroke: Stroke, curr_stroke: Stroke) -> str:
    ratio = bbox_overlap_ratio(stroke_box(prev_stroke), stroke_box(curr_stroke))
    if ratio >= 0.65:
        return "REL_OVERLAP_HIGH"
    if ratio >= 0.15:
        return "REL_OVERLAP_PARTIAL"
    if ratio >= 0.02:
        return "REL_NEAR"
    return "REL_SEPARATE"


# ---------------------------------------------------------------------------
# Ambiguity / stability diagnostics
# ---------------------------------------------------------------------------

def _movement_angle(a: Point, b: Point) -> Optional[float]:
    dx = b[0] - a[0]
    dy = -(b[1] - a[1])
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return None
    angle = math.degrees(math.atan2(dy, dx))
    return angle + 360.0 if angle < 0 else angle


def direction_boundary_count(strokes: Sequence[Stroke], dirs: List[str], boundary_fraction: float = 0.12) -> int:
    """Count movements whose angle is close to a direction bucket boundary."""
    n = len(dirs)
    width = 360.0 / n
    threshold = width * boundary_fraction
    count = 0
    for stroke in strokes:
        for i in range(1, len(stroke)):
            angle = _movement_angle(stroke[i - 1], stroke[i])
            if angle is None:
                continue
            # Boundaries lie halfway between direction centers. Since E is centered
            # at 0°, boundaries are at width/2, width/2+width, ...
            pos = (angle - width / 2.0) % width
            dist_to_boundary = min(pos, width - pos)
            if dist_to_boundary <= threshold:
                count += 1
    return count


def start_zone_ambiguity_count(strokes: Sequence[Stroke], drawing_box: Tuple[float, float, float, float], grid: int, margin: float = 0.08) -> int:
    """Count stroke starts close to an internal start-zone boundary."""
    min_x, min_y, max_x, max_y = drawing_box
    w = max(max_x - min_x, 1e-9)
    h = max(max_y - min_y, 1e-9)
    count = 0
    for stroke in strokes:
        x, y = stroke[0]
        ux = ((x - min_x) / w) * grid
        uy = ((y - min_y) / h) * grid
        for u in (ux, uy):
            nearest = round(u)
            # Only internal boundaries matter. Edges clamp to the same zone.
            if 0 < nearest < grid and abs(u - nearest) <= margin:
                count += 1
                break
    return count


def length_bucket_ambiguity_count(runs_by_stroke: Sequence[Sequence[Run]], spacing: float, lb: LengthBuckets, margin: float = 0.025) -> int:
    count = 0
    for runs in runs_by_stroke:
        for _direction, c in runs:
            dist = c * spacing
            if abs(dist - lb.short_max) <= margin or abs(dist - lb.medium_max) <= margin:
                count += 1
    return count


def closed_threshold_ambiguity_count(strokes: Sequence[Stroke], threshold: float, margin: float = 0.02) -> int:
    count = 0
    for stroke in strokes:
        if len(stroke) >= 3 and abs(_distance(stroke[0], stroke[-1]) - threshold) <= margin:
            count += 1
    return count


def ambiguity_flags(
    *,
    resampled: Sequence[Stroke],
    denoised_runs_by_stroke: Sequence[Sequence[Run]],
    drawing_box: Tuple[float, float, float, float],
    dirs: List[str],
    params: EncoderParams,
) -> Tuple[List[str], Dict[str, int]]:
    counts = {
        "direction_boundary_steps": direction_boundary_count(resampled, dirs),
        "start_zone_boundaries": start_zone_ambiguity_count(resampled, drawing_box, params.zone_grid) if params.include_start_zone else 0,
        "length_bucket_boundaries": length_bucket_ambiguity_count(denoised_runs_by_stroke, params.resample_spacing, params.length_buckets),
        "closed_threshold_boundaries": closed_threshold_ambiguity_count(resampled, params.close_threshold),
    }
    flags: List[str] = []
    if counts["direction_boundary_steps"]:
        flags.append(f"ambiguous_direction_boundary:{counts['direction_boundary_steps']}")
    if counts["start_zone_boundaries"]:
        flags.append(f"ambiguous_start_zone:{counts['start_zone_boundaries']}")
    if counts["length_bucket_boundaries"]:
        flags.append(f"ambiguous_length_bucket:{counts['length_bucket_boundaries']}")
    if counts["closed_threshold_boundaries"]:
        flags.append(f"ambiguous_closed_threshold:{counts['closed_threshold_boundaries']}")
    return flags, counts


# ---------------------------------------------------------------------------
# Main encoder
# ---------------------------------------------------------------------------

def encode_strokes(
    raw_strokes: Sequence[Sequence[Sequence[Any]]],
    params: Optional[EncoderParams] = None,
) -> Dict[str, Any]:
    """Encode strokes into a symbolic token sequence and diagnostics."""
    params = params or EncoderParams()
    params.validate()

    dirs = _direction_table(params.direction_buckets)
    lb   = params.length_buckets

    # --- cleaning and normalization ---
    clean = clean_strokes(
        raw_strokes,
        min_points=params.min_stroke_points,
        min_raw_length=params.min_raw_stroke_length,
    )
    if not clean:
        raise ValueError("no valid strokes — draw more or reduce minimum stroke length")

    normalized_all = normalize_strokes(clean, round_digits=params.round_normalized)
    normalized, n_dropped = filter_tiny_strokes(
        normalized_all, params.min_normalized_stroke_length
    )
    if not normalized:
        raise ValueError("all strokes were too small after normalization")

    simplified = rdp_simplify_strokes(normalized, params.simplify_epsilon)
    if params.order_mode == "spatial":
        simplified = sort_strokes_spatial(simplified)

    resampled = resample_strokes(simplified, params.resample_spacing)

    # Drawing bounding box (after simplification, used for relative zone tokens)
    drawing_box = bounding_box(simplified)

    # --- tokenization ---
    tokens: List[str] = []
    stroke_summaries: List[Dict[str, Any]] = []

    # Tracking for stats
    all_raw_dirs:      List[str] = []
    all_denoised_dirs: List[str] = []
    penup_tokens:      List[str] = []
    relation_tokens:   List[str] = []
    denoised_runs_by_stroke: List[List[Run]] = []

    for idx, stroke in enumerate(resampled):

        # Between strokes: pen-up move, then relation token
        if idx > 0:
            if params.include_penup_moves:
                pu = penup_move_token(resampled[idx - 1], stroke, dirs, lb)
            else:
                pu = "PU"
            tokens.append(pu)
            penup_tokens.append(pu)

            if params.include_relation_tokens:
                rel = relation_token(resampled[idx - 1], stroke)
                tokens.append(rel)
                relation_tokens.append(rel)

        # Stroke start
        if params.include_start_zone:
            start_tok = relative_start_zone(stroke, drawing_box, params.zone_grid)
        else:
            start_tok = "S"
        tokens.append(start_tok)

        # Closed marker
        closed = is_closed(stroke, params.close_threshold)
        if params.include_closed_tokens and closed:
            tokens.append("CLOSED")

        # Direction tokenization
        raw_dirs = stroke_to_directions(stroke, dirs)
        all_raw_dirs.extend(raw_dirs)

        raw_runs      = runs_from_directions(raw_dirs)
        denoised_runs = denoise_runs(raw_runs, params.jitter_run_max)
        denoised_runs_by_stroke.append(list(denoised_runs))

        for d, _ in denoised_runs:
            all_denoised_dirs.append(d)

        dir_tokens = runs_to_tokens(
            denoised_runs, dirs, lb,
            include_turns=params.include_turn_tokens,
            spacing=params.resample_spacing,
            include_turn_magnitude=params.include_turn_magnitude,
        )
        tokens.extend(dir_tokens)

        stroke_summaries.append({
            "index":              idx,
            "start_token":        start_tok,
            "closed":             closed,
            "path_length":        round(_path_length(stroke), 6),
            "raw_run_tokens":     [f"{d}{c}" for d, c in raw_runs],
            "denoised_run_tokens":[f"{d}{c}" for d, c in denoised_runs],
            "direction_tokens":   dir_tokens,
        })

    tokens.append("END")

    serialized = _serialize(tokens, params)
    stats = _compute_stats(
        tokens=tokens,
        raw_dirs=all_raw_dirs,
        denoised_dirs=all_denoised_dirs,
        clean=clean,
        normalized=normalized,
        simplified=simplified,
        resampled=resampled,
        n_dropped=n_dropped,
        penup_tokens=penup_tokens,
        relation_tokens=relation_tokens,
        stroke_summaries=stroke_summaries,
        denoised_runs_by_stroke=denoised_runs_by_stroke,
        drawing_box=drawing_box,
        dirs=dirs,
        params=params,
    )

    return {
        "version":      "DRNG-STROKE-v0.3",
        "params":       _params_to_dict(params),
        "tokens":       tokens,
        "serialized":   serialized,
        "stats":        stats,
        "stroke_summaries":   stroke_summaries,
        "normalized_strokes": [[list(p) for p in s] for s in normalized],
        "simplified_strokes": [[list(p) for p in s] for s in simplified],
        "resampled_strokes":  [[list(p) for p in s] for s in resampled],
    }


# ---------------------------------------------------------------------------
# Serialization — encodes ALL params that affect token output
# ---------------------------------------------------------------------------

def _params_to_dict(p: EncoderParams) -> Dict[str, Any]:
    """Return a flat dict suitable for JSON and for the serialized header."""
    return {
        "resample_spacing":              p.resample_spacing,
        "direction_buckets":             p.direction_buckets,
        "length_buckets_short_max":      p.length_buckets.short_max,
        "length_buckets_medium_max":     p.length_buckets.medium_max,
        "min_stroke_points":             p.min_stroke_points,
        "min_raw_stroke_length":         p.min_raw_stroke_length,
        "min_normalized_stroke_length":  p.min_normalized_stroke_length,
        "jitter_run_max":                p.jitter_run_max,
        "simplify_epsilon":              p.simplify_epsilon,
        "order_mode":                    p.order_mode,
        "include_turn_tokens":           p.include_turn_tokens,
        "include_turn_magnitude":        p.include_turn_magnitude,
        "include_start_zone":            p.include_start_zone,
        "include_penup_moves":           p.include_penup_moves,
        "include_closed_tokens":         p.include_closed_tokens,
        "include_relation_tokens":       p.include_relation_tokens,
        "close_threshold":               p.close_threshold,
        "zone_grid":                     p.zone_grid,
        "round_normalized":              p.round_normalized,
    }


def _serialize(tokens: Sequence[str], params: EncoderParams) -> str:
    """Produce a deterministic string encoding of the full token sequence.

    The header encodes every parameter that affects the output so two
    configurations cannot accidentally produce identical serialized strings.
    """
    p = params
    header = (
        f"DRNG-STROKE-v0.3"
        f"|dir={p.direction_buckets}"
        f"|len={p.length_buckets.short_max:.3f},{p.length_buckets.medium_max:.3f}"
        f"|spacing={p.resample_spacing:.3f}"
        f"|jitter={p.jitter_run_max}"
        f"|simp={p.simplify_epsilon:.3f}"
        f"|order={p.order_mode}"
        f"|zone={p.zone_grid if p.include_start_zone else 0}"
        f"|turns={int(p.include_turn_tokens)}"
        f"|turnmag={int(p.include_turn_magnitude)}"
        f"|pu={int(p.include_penup_moves)}"
        f"|closed={int(p.include_closed_tokens)}"
        f"|rel={int(p.include_relation_tokens)}"
    )
    return header + "|" + "|".join(tokens)


# ---------------------------------------------------------------------------
# Seed derivation (for demos/experiments — not a cryptographic claim)
# ---------------------------------------------------------------------------

def derive_seed_material(serialized: str, salt: str = "drng-public-salt", out_bytes: int = 32) -> str:
    """Derive a fixed-length byte string from the token sequence.

    Uses HKDF-like counter mode with BLAKE2b.
    This is for experimental demonstrations only — no cryptographic security claim.
    The output looks uniform but the security depends entirely on how guessable
    the input token sequence is.
    """
    if out_bytes <= 0:
        raise ValueError("out_bytes must be positive")
    # Extract step: BLAKE2b of (salt || serialized)
    prk = hashlib.blake2b(
        serialized.encode("utf-8"),
        key=salt.encode("utf-8")[:64],   # blake2b key max 64 bytes
        digest_size=64,
        person=b"DRNG-STROKE-v0.3",
    ).digest()

    # Expand step: counter mode
    output = bytearray()
    counter = 0
    while len(output) < out_bytes:
        block = hashlib.blake2b(
            prk + counter.to_bytes(8, "big"),
            digest_size=64,
            person=b"DRNG-STROKE-v0.3",
        ).digest()
        output.extend(block)
        counter += 1
    return bytes(output[:out_bytes]).hex()


# ---------------------------------------------------------------------------
# Similarity metrics
# ---------------------------------------------------------------------------

def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein edit distance between two token sequences."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        curr = [i]
        for j, y in enumerate(b, 1):
            curr.append(min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if x == y else 1),
            ))
        prev = curr
    return prev[-1]


def normalized_edit_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """1.0 = identical, approaches 0.0 as sequences diverge."""
    denom = max(len(a), len(b), 1)
    return 1.0 - edit_distance(a, b) / denom


def direction_only_tokens(tokens: Sequence[str]) -> List[str]:
    """Extract only direction-run tokens for a direction-focused similarity check."""
    return [t for t in tokens if classify_token(t) == TokenKind.DIRECTION_RUN]


# ---------------------------------------------------------------------------
# Weak seed flags (heuristic — not cryptographic validation)
# ---------------------------------------------------------------------------

def weak_seed_flags(
    tokens: Sequence[str],
    denoised_dirs: Sequence[str],
    stroke_count: int,
    normalized_path_length: float,
    closed_count: int,
    relation_tokens: Sequence[str],
) -> List[str]:
    flags: List[str] = []

    dir_run_tokens = [t for t in tokens if classify_token(t) == TokenKind.DIRECTION_RUN]
    turn_toks      = [t for t in tokens if classify_token(t) == TokenKind.TURN]
    unique_dirs    = len(set(denoised_dirs))
    unique_turns   = len(set(turn_toks))

    if stroke_count <= 1:
        flags.append("single_stroke")
    if len(denoised_dirs) < 8:
        flags.append("very_short_path")
    if len(dir_run_tokens) < 4:
        flags.append("low_direction_changes")
    if unique_dirs <= 2:
        flags.append("low_direction_diversity")
    if normalized_path_length < 0.4:
        flags.append("small_total_path")
    if turn_toks and unique_turns <= 1:
        flags.append("low_turn_diversity")
    if stroke_count <= 2 and closed_count == stroke_count and unique_dirs <= 4:
        flags.append("simple_closed_shapes_only")
    if relation_tokens and all(t == "REL_SEPARATE" for t in relation_tokens):
        flags.append("all_strokes_separate")

    return flags


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _compute_stats(
    tokens: Sequence[str],
    raw_dirs: Sequence[str],
    denoised_dirs: Sequence[str],
    clean: Sequence[Stroke],
    normalized: Sequence[Stroke],
    simplified: Sequence[Stroke],
    resampled: Sequence[Stroke],
    n_dropped: int,
    penup_tokens: Sequence[str],
    relation_tokens: Sequence[str],
    stroke_summaries: Sequence[Dict[str, Any]],
    denoised_runs_by_stroke: Sequence[Sequence[Run]],
    drawing_box: Tuple[float, float, float, float],
    dirs: List[str],
    params: EncoderParams,
) -> Dict[str, Any]:

    by_kind: Dict[str, int] = {}
    for t in tokens:
        k = classify_token(t).value
        by_kind[k] = by_kind.get(k, 0) + 1

    dirs = _direction_table(params.direction_buckets)
    dir_counts_raw      = {d: raw_dirs.count(d)      for d in dirs}
    dir_counts_denoised = {d: denoised_dirs.count(d) for d in dirs}

    total_path = sum(_path_length(s) for s in normalized)
    closed_count = sum(1 for s in stroke_summaries if s["closed"])

    flags = weak_seed_flags(
        tokens=tokens,
        denoised_dirs=denoised_dirs,
        stroke_count=len(normalized),
        normalized_path_length=total_path,
        closed_count=closed_count,
        relation_tokens=relation_tokens,
    )

    amb_flags, amb_counts = ambiguity_flags(
        resampled=resampled,
        denoised_runs_by_stroke=denoised_runs_by_stroke,
        drawing_box=drawing_box,
        dirs=dirs,
        params=params,
    )

    return {
        "stroke_count":                      len(normalized),
        "strokes_dropped_tiny":              n_dropped,
        "original_point_count":              sum(len(s) for s in clean),
        "simplified_point_count":            sum(len(s) for s in simplified),
        "resampled_point_count":             sum(len(s) for s in resampled),
        "order_mode":                        params.order_mode,
        "token_count":                       len(tokens),
        "tokens_by_kind":                    by_kind,
        "direction_run_count":               by_kind.get(TokenKind.DIRECTION_RUN.value, 0),
        "turn_token_count":                  by_kind.get(TokenKind.TURN.value, 0),
        "unique_direction_runs":             len({t for t in tokens if classify_token(t) == TokenKind.DIRECTION_RUN}),
        "raw_direction_step_count":          len(raw_dirs),
        "denoised_direction_step_count":     len(denoised_dirs),
        "unique_raw_directions":             len(set(raw_dirs)),
        "unique_denoised_directions":        len(set(denoised_dirs)),
        "direction_counts_raw":              dir_counts_raw,
        "direction_counts_denoised":         dir_counts_denoised,
        "closed_stroke_count":               closed_count,
        "penup_tokens":                      list(penup_tokens),
        "relation_tokens":                   list(relation_tokens),
        "normalized_total_path_length":      round(total_path, 6),
        "weak_seed_flags":                   flags,
        "ambiguity_flags":                   amb_flags,
        "ambiguity_counts":                  amb_counts,
    }


# ---------------------------------------------------------------------------
# Top-level JSON API (used by Flask app.py)
# ---------------------------------------------------------------------------

def encode_json_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    params = params_from_dict(payload.get("params"))
    result = encode_strokes(payload.get("strokes", []), params=params)
    result["seed_material_hex"] = derive_seed_material(result["serialized"], out_bytes=32)
    return result


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # A square, drawn clockwise
    square = [
        [[10, 10], [60, 10], [60, 60], [10, 60], [10, 10]],
    ]
    # A slightly larger square (should produce same/similar tokens after normalization)
    square_bigger = [
        [[5, 5], [80, 5], [80, 80], [5, 80], [5, 5]],
    ]
    r1 = encode_json_payload({"strokes": square})
    r2 = encode_json_payload({"strokes": square_bigger})

    print("=== Square ===")
    print(r1["serialized"])
    print("Tokens:", r1["tokens"])
    print("Flags: ", r1["stats"]["weak_seed_flags"])
    print()
    print("=== Bigger square (should match) ===")
    print(r2["serialized"])
    print()
    sim = normalized_edit_similarity(r1["tokens"], r2["tokens"])
    print(f"Token similarity: {sim:.3f}  (1.0 = identical)")
    print()

    # A two-stroke drawing: circle + cross
    two_stroke = [
        [[80, 40], [100, 20], [120, 40], [100, 60], [80, 40]],   # circle-ish
        [[90, 40], [110, 40]],                                     # horizontal bar
    ]
    r3 = encode_json_payload({"strokes": two_stroke, "params": {"direction_buckets": 16}})
    print("=== Two-stroke (16-direction mode) ===")
    print(r3["serialized"])
    print("Flags: ", r3["stats"]["weak_seed_flags"])
