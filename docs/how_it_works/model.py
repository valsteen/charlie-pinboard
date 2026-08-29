from dataclasses import dataclass
from html import escape
from typing import Literal

type Emphasis = Literal["primary", "muted"]
type TextAnchor = Literal["start", "middle", "end"]


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
    subtitle = (
        ""
        if not value.subtitle
        else f'<text x="{value.x}" y="{value.y + 22}" class="section-subtitle">{escape(value.subtitle)}</text>'
    )
    return f'<text x="{value.x}" y="{value.y}" class="section-label">{escape(value.label.upper())}</text>{subtitle}'


def _guide(value: Guide) -> str:
    return f'<line x1="{value.start[0]}" y1="{value.start[1]}" x2="{value.end[0]}" y2="{value.end[1]}" class="guide"/>'


def _connector(value: Connector) -> str:
    dash = ' stroke-dasharray="7 7"' if value.dashed else ""
    marker = ' marker-end="url(#arrow)"' if value.arrow else ""
    path = f'<path d="{_orthogonal_path(value.points)}" class="connector"{dash}{marker}/>'
    if not value.label:
        return path
    if value.label_position is None:
        raise AssertionError("validated connector label position is missing")
    return (
        f'{path}<text x="{value.label_position[0]}" y="{value.label_position[1]}" '
        f'text-anchor="middle" class="connector-label">{escape(value.label.upper())}</text>'
    )


def _box(value: Box) -> str:
    accent = "#394a62" if value.emphasis == "primary" else "#81796f"
    title_y = value.y + (49 if value.label else 28)
    body = [
        f'<rect x="{value.x}" y="{value.y}" width="{value.width}" height="{value.height}" class="box"/>',
        f'<rect x="{value.x}" y="{value.y}" width="3" height="{value.height}" fill="{accent}"/>',
    ]
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
        f'font-size="{value.size}" class="{css_class}">{escape(value.text)}</text>'
    )


def render_svg(diagram: Diagram, source_revision: str) -> str:
    validate_diagram(diagram)
    body = "".join(
        (
            *(_section(value) for value in diagram.sections),
            *(_guide(value) for value in diagram.guides),
            *(_connector(value) for value in diagram.connectors),
            *(_box(value) for value in diagram.boxes),
            *(_note(value) for value in diagram.notes),
        )
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title description" viewBox="0 0 {diagram.width} {diagram.height}">
  <title id="title">{escape(diagram.title)}</title>
  <desc id="description">{escape(diagram.description)}</desc>
  <metadata>Generated by docs.how_it_works; source revision {source_revision}</metadata>
  <defs>
    <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse">
      <path d="M24 0H0V24" fill="none" stroke="#e8e1d5" stroke-width="1"/>
    </pattern>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M1 1L9 5L1 9Z" fill="#697487"/>
    </marker>
    <style>
      text {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #292721; }}
      .section-label, .box-label, .box-meta, .connector-label, .note-meta {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
      .section-label, .box-label {{ font-size: 11px; font-weight: 700; letter-spacing: 0.11em; fill: #6d665b; }}
      .section-subtitle {{ font-size: 11px; fill: #625d53; }}
      .guide {{ stroke: #d3ccc0; stroke-width: 1; }}
      .connector {{ fill: none; stroke: #697487; stroke-width: 1.9; stroke-linecap: round; }}
      .connector-label {{ font-size: 10px; font-weight: 700; fill: #697487; }}
      .box {{ fill: #fff; stroke: #aaa397; stroke-width: 1.2; }}
      .box-title {{ font-size: 15px; font-weight: 700; }}
      .box-body {{ font-size: 11px; fill: #625d53; }}
      .box-meta {{ font-size: 10px; fill: #697487; }}
      .note {{ fill: #625d53; }}
      .note-meta {{ font-weight: 700; letter-spacing: 0.08em; fill: #6d665b; }}
    </style>
  </defs>
  <rect width="{diagram.width}" height="{diagram.height}" fill="#fffdf7"/>
  <rect width="{diagram.width}" height="{diagram.height}" fill="url(#grid)"/>
  {body}
  <rect x="0.5" y="0.5" width="{diagram.width - 1}" height="{diagram.height - 1}" fill="none" stroke="#cec7b9"/>
</svg>
'''
