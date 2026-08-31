from dataclasses import dataclass
from html import escape
from typing import Literal

type Emphasis = Literal["primary", "muted"]
type TextAnchor = Literal["start", "middle", "end"]


@dataclass(frozen=True)
class Palette:
    canvas: str
    grid: str
    frame: str
    card: str
    card_border: str
    text: str
    label: str
    muted_text: str
    guide: str
    connector: str
    primary: str
    secondary: str


DAY_PALETTE = Palette(
    canvas="#fffdf7",
    grid="#e8e1d5",
    frame="#cec7b9",
    card="#fff",
    card_border="#aaa397",
    text="#292721",
    label="#6d665b",
    muted_text="#625d53",
    guide="#d3ccc0",
    connector="#697487",
    primary="#394a62",
    secondary="#81796f",
)

NIGHT_PALETTE = Palette(
    canvas="#1c2127",
    grid="#45535f",
    frame="#45515c",
    card="#242b32",
    card_border="#5e6a74",
    text="#edf1f2",
    label="#b7c0c5",
    muted_text="#c5cdd0",
    guide="#333b42",
    connector="#9ba8b1",
    primary="#91a7bb",
    secondary="#9b948b",
)


@dataclass(frozen=True)
class Box:
    key: str
    label: str
    title: str
    details: tuple[str, ...]
    meta: tuple[str, ...]
    x: int
    y: int
    width: int
    height: int
    emphasis: Emphasis = "primary"


@dataclass(frozen=True)
class Section:
    label: str
    subtitle: str
    x: int
    y: int


@dataclass(frozen=True)
class Guide:
    start: tuple[int, int]
    end: tuple[int, int]


@dataclass(frozen=True)
class Connector:
    points: tuple[tuple[int, int], ...]
    source: str | None
    target: str | None
    label: str = ""
    label_position: tuple[int, int] | None = None
    dashed: bool = False
    arrow: bool = True


@dataclass(frozen=True)
class Note:
    text: str
    x: int
    y: int
    size: int = 12
    anchor: TextAnchor = "start"
    meta: bool = False


@dataclass(frozen=True)
class Diagram:
    slug: str
    title: str
    description: str
    width: int
    height: int
    sections: tuple[Section, ...]
    guides: tuple[Guide, ...]
    connectors: tuple[Connector, ...]
    boxes: tuple[Box, ...]
    notes: tuple[Note, ...] = ()


_CORNER_RADIUS = 12
_PORT_MARGIN = 12
_ARROW_APPROACH = 16
_CARD_CLEARANCE = 12
_BOX_TEXT_INSET = 16
_BOX_TEXT_RIGHT_MARGIN = 16
_TEXT_WIDTH_FACTORS = {
    "label": 7.5,
    "meta": 6.2,
}
_NARROW_GLYPHS = frozenset(" !'(),.:;I[]`fijlt|")
_WIDE_GLYPHS = frozenset("%&@GMOQVWmw")


def _segment_length(start: tuple[int, int], end: tuple[int, int]) -> int:
    return abs(end[0] - start[0]) + abs(end[1] - start[1])


def _is_orthogonal(start: tuple[int, int], end: tuple[int, int]) -> bool:
    return (start[0] == end[0]) != (start[1] == end[1])


def _towards(start: tuple[int, int], end: tuple[int, int], distance: int) -> tuple[int, int]:
    if start[0] == end[0]:
        direction = 1 if end[1] > start[1] else -1
        return start[0], start[1] + direction * distance
    direction = 1 if end[0] > start[0] else -1
    return start[0] + direction * distance, start[1]


def _stable_port(box: Box, point: tuple[int, int]) -> bool:
    x, y = point
    horizontal_edge = y in (box.y, box.y + box.height) and box.x + _PORT_MARGIN <= x <= box.x + box.width - _PORT_MARGIN
    vertical_edge = x in (box.x, box.x + box.width) and box.y + _PORT_MARGIN <= y <= box.y + box.height - _PORT_MARGIN
    return horizontal_edge or vertical_edge


def _text_fits_width(box: Box, text: str, style: str) -> bool:
    available = box.width - _BOX_TEXT_INSET - _BOX_TEXT_RIGHT_MARGIN
    if style in _TEXT_WIDTH_FACTORS:
        estimated = len(text) * _TEXT_WIDTH_FACTORS[style]
    else:
        em_width = sum(
            0.34 if character in _NARROW_GLYPHS else 0.78 if character in _WIDE_GLYPHS else 0.56 for character in text
        )
        estimated = em_width * (15.9 if style == "title" else 11)
    return estimated <= available


def _segment_enters_clearance(start: tuple[int, int], end: tuple[int, int], box: Box) -> bool:
    left = box.x - _CARD_CLEARANCE
    right = box.x + box.width + _CARD_CLEARANCE
    top = box.y - _CARD_CLEARANCE
    bottom = box.y + box.height + _CARD_CLEARANCE
    if start[1] == end[1]:
        segment_left, segment_right = sorted((start[0], end[0]))
        return top <= start[1] <= bottom and segment_left <= right and segment_right >= left
    segment_top, segment_bottom = sorted((start[1], end[1]))
    return left <= start[0] <= right and segment_top <= bottom and segment_bottom >= top


def _validate_connector_clearance(
    diagram: Diagram,
    connector: Connector,
    boxes: dict[str, Box],
    segments: tuple[tuple[tuple[int, int], tuple[int, int]], ...],
) -> None:
    for box in boxes.values():
        for index, (start, end) in enumerate(segments):
            leaves_source = box.key == connector.source and index == 0
            enters_target = box.key == connector.target and index == len(segments) - 1
            if not leaves_source and not enters_target and _segment_enters_clearance(start, end, box):
                raise ValueError(f"{diagram.slug} connector needs clearance from {box.key}")


def _validate_box(diagram: Diagram, box: Box) -> None:
    if box.x < 0 or box.y < 0 or box.x + box.width > diagram.width or box.y + box.height > diagram.height:
        raise ValueError(f"{diagram.slug} box {box.key} leaves the canvas")
    title_y = 49 if box.label else 28
    last_detail_y = title_y + len(box.details) * 21
    first_meta_y = box.height - 14 - max(0, len(box.meta) - 1) * 14
    if box.meta and last_detail_y > first_meta_y - 14:
        raise ValueError(f"{diagram.slug} box {box.key} text does not fit")
    if not box.meta and last_detail_y > box.height - 12:
        raise ValueError(f"{diagram.slug} box {box.key} text does not fit")
    text_lines = [(box.title, "title")]
    if box.label:
        text_lines.append((box.label, "label"))
    text_lines.extend((line, "body") for line in box.details)
    text_lines.extend((line, "meta") for line in box.meta)
    if any(not _text_fits_width(box, text, style) for text, style in text_lines):
        raise ValueError(f"{diagram.slug} box {box.key} text needs a right margin")


def _validate_connector(diagram: Diagram, connector: Connector, boxes: dict[str, Box]) -> None:
    if len(connector.points) < 2:
        raise ValueError(f"{diagram.slug} connector needs at least two points")
    segments = tuple(zip(connector.points, connector.points[1:], strict=False))
    if any(not _is_orthogonal(start, end) for start, end in segments):
        raise ValueError(f"{diagram.slug} connector routes must be orthogonal")
    if any(_segment_length(start, end) < 1 for start, end in segments):
        raise ValueError(f"{diagram.slug} connector contains a zero-length segment")
    if connector.arrow and _segment_length(*segments[-1]) < _ARROW_APPROACH + (
        0 if len(segments) == 1 else _CORNER_RADIUS
    ):
        raise ValueError(f"{diagram.slug} connector arrowhead needs a straight approach")
    if connector.label and connector.label_position is None:
        raise ValueError(f"{diagram.slug} connector labels need an explicit position")
    if connector.source is not None:
        source = boxes.get(connector.source)
        if source is None or not _stable_port(source, connector.points[0]):
            raise ValueError(f"{diagram.slug} connector source {connector.source} is not a stable card port")
    if connector.target is not None:
        target = boxes.get(connector.target)
        if target is None or not _stable_port(target, connector.points[-1]):
            raise ValueError(f"{diagram.slug} connector target {connector.target} is not a stable card port")
    _validate_connector_clearance(diagram, connector, boxes, segments)


def validate_diagram(diagram: Diagram) -> None:
    boxes = {box.key: box for box in diagram.boxes}
    if len(boxes) != len(diagram.boxes):
        raise ValueError(f"{diagram.slug} diagram has duplicate box keys")
    for box in diagram.boxes:
        _validate_box(diagram, box)
    for connector in diagram.connectors:
        _validate_connector(diagram, connector, boxes)


def _orthogonal_path(points: tuple[tuple[int, int], ...]) -> str:
    if len(points) == 2:
        return f"M{points[0][0]} {points[0][1]}L{points[1][0]} {points[1][1]}"
    parts = [f"M{points[0][0]} {points[0][1]}"]
    for index, corner in enumerate(points[1:-1], start=1):
        previous = points[index - 1]
        following = points[index + 1]
        radius = min(_CORNER_RADIUS, _segment_length(previous, corner) // 2, _segment_length(corner, following) // 2)
        before = _towards(corner, previous, radius)
        after = _towards(corner, following, radius)
        parts.append(f"L{before[0]} {before[1]}Q{corner[0]} {corner[1]} {after[0]} {after[1]}")
    parts.append(f"L{points[-1][0]} {points[-1][1]}")
    return "".join(parts)


def _section(value: Section) -> str:
    subtitles = "".join(
        f'<text x="{value.x}" y="{value.y + 22 + index * 16}" class="section-subtitle canvas-text">'
        f"{escape(line)}</text>"
        for index, line in enumerate(value.subtitle.splitlines())
    )
    return (
        f'<text x="{value.x}" y="{value.y}" class="section-label canvas-text">'
        f"{escape(value.label.upper())}</text>{subtitles}"
    )


def _guide(value: Guide) -> str:
    return f'<line x1="{value.start[0]}" y1="{value.start[1]}" x2="{value.end[0]}" y2="{value.end[1]}" class="guide"/>'


def _connector_path(value: Connector) -> str:
    dash = ' stroke-dasharray="7 7"' if value.dashed else ""
    marker = ' marker-end="url(#arrow)"' if value.arrow else ""
    return f'<path d="{_orthogonal_path(value.points)}" class="connector"{dash}{marker}/>'


def _connector_label(value: Connector) -> str:
    if not value.label:
        return ""
    if value.label_position is None:
        raise AssertionError("validated connector label position is missing")
    return (
        f'<text x="{value.label_position[0]}" y="{value.label_position[1]}" '
        f'text-anchor="middle" class="connector-label canvas-text">{escape(value.label.upper())}</text>'
    )


def _box_surface(value: Box, palette: Palette) -> str:
    accent = palette.primary if value.emphasis == "primary" else palette.secondary
    return (
        f'<rect x="{value.x}" y="{value.y}" width="{value.width}" height="{value.height}" class="box"/>'
        f'<rect x="{value.x}" y="{value.y}" width="3" height="{value.height}" fill="{accent}"/>'
    )


def _box_text(value: Box) -> str:
    title_y = value.y + (49 if value.label else 28)
    body: list[str] = []
    if value.label:
        body.append(
            f'<text x="{value.x + 16}" y="{value.y + 22}" class="box-label">{escape(value.label.upper())}</text>'
        )
    body.append(f'<text x="{value.x + 16}" y="{title_y}" class="box-title">{escape(value.title)}</text>')
    for index, line in enumerate(value.details, start=1):
        body.append(f'<text x="{value.x + 16}" y="{title_y + index * 21}" class="box-body">{escape(line)}</text>')
    meta_start = value.y + value.height - 14 - max(0, len(value.meta) - 1) * 14
    for index, line in enumerate(value.meta):
        body.append(f'<text x="{value.x + 16}" y="{meta_start + index * 14}" class="box-meta">{escape(line)}</text>')
    return "".join(body)


def _note(value: Note) -> str:
    css_class = "note-meta" if value.meta else "note"
    return (
        f'<text x="{value.x}" y="{value.y}" text-anchor="{value.anchor}" '
        f'font-size="{value.size}" class="{css_class} canvas-text">{escape(value.text)}</text>'
    )


def render_svg(diagram: Diagram, source_revision: str, palette: Palette) -> str:
    validate_diagram(diagram)
    geometry = "".join(
        (
            *(_guide(value) for value in diagram.guides),
            *(_connector_path(value) for value in diagram.connectors),
            *(_box_surface(value, palette) for value in diagram.boxes),
        )
    )
    text = "".join(
        (
            *(_section(value) for value in diagram.sections),
            *(_connector_label(value) for value in diagram.connectors),
            *(_box_text(value) for value in diagram.boxes),
            *(_note(value) for value in diagram.notes),
        )
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title description" viewBox="0 0 {diagram.width} {diagram.height}">
  <title id="title">{escape(diagram.title)}</title>
  <desc id="description">{escape(diagram.description)}</desc>
  <metadata>Generated by docs.how_it_works; source revision {source_revision}</metadata>
  <defs>
    <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse">
      <path d="M24 0H0V24" fill="none" stroke="{palette.grid}" stroke-width="1"/>
    </pattern>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M1 1L9 5L1 9Z" fill="{palette.connector}"/>
    </marker>
    <style>
      text {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: {palette.text}; }}
      .section-label, .box-label, .box-meta, .connector-label, .note-meta {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
      .section-label, .box-label {{ font-size: 11px; font-weight: 700; letter-spacing: 0.11em; fill: {palette.label}; }}
      .section-subtitle {{ font-size: 11px; fill: {palette.muted_text}; }}
      .guide {{ stroke: {palette.guide}; stroke-width: 1; }}
      .connector {{ fill: none; stroke: {palette.connector}; stroke-width: 1.9; stroke-linecap: round; }}
      .connector-label {{ font-size: 10px; font-weight: 700; fill: {palette.connector}; }}
      .box {{ fill: {palette.card}; stroke: {palette.card_border}; stroke-width: 1.2; }}
      .box-title {{ font-size: 15px; font-weight: 700; }}
      .box-body {{ font-size: 11px; fill: {palette.muted_text}; }}
      .box-meta {{ font-size: 10px; fill: {palette.connector}; }}
      .note {{ fill: {palette.muted_text}; }}
      .note-meta {{ font-weight: 700; letter-spacing: 0.08em; fill: {palette.label}; }}
      .canvas-text {{ paint-order: stroke fill; stroke: {palette.canvas}; stroke-width: 4px; stroke-linejoin: round; }}
    </style>
  </defs>
  <rect width="{diagram.width}" height="{diagram.height}" fill="{palette.canvas}"/>
  <rect width="{diagram.width}" height="{diagram.height}" fill="url(#grid)"/>
  <rect x="0.5" y="0.5" width="{diagram.width - 1}" height="{diagram.height - 1}" fill="none" stroke="{palette.frame}"/>
  {geometry}
  {text}
</svg>
'''
