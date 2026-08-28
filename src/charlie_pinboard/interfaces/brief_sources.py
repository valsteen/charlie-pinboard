import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Final

import msgspec

from charlie_pinboard.interfaces.brief_source_models import (
    AuthoritySelector,
    BriefSourceBatch,
    BriefSourceLine,
    BriefSourceManifest,
    BriefSourcePlan,
    BriefSourceRequest,
    BriefSourceSegment,
    PlannedBriefSource,
    SelectedBriefSource,
)
from charlie_pinboard.interfaces.errors import BriefSourceError, BriefSourceErrorCode

MARKDOWN_HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_authority_selector(value: str) -> AuthoritySelector:
    relative, separator, heading = value.partition("#")
    relative_path = PurePosixPath(relative)
    if (
        not relative
        or "\x00" in value
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or not relative_path.parts
        or (separator and not heading)
    ):
        raise BriefSourceError(
            BriefSourceErrorCode.MANIFEST_INVALID,
            f"Authority selector '{value}' must name one project-relative file and optional literal heading.",
        )
    return AuthoritySelector(relative_path, heading if separator else None)


def decode_brief_source_manifest(raw: bytes) -> BriefSourceManifest:
    try:
        manifest = msgspec.json.decode(raw, type=BriefSourceManifest)
    except (msgspec.DecodeError, ValueError) as error:
        raise BriefSourceError(
            BriefSourceErrorCode.MANIFEST_INVALID,
            f"Cannot decode brief source manifest: {error}",
        ) from error
    for source in manifest.sources:
        parse_authority_selector(source.selector)
    return manifest


def _selected_heading(lines: tuple[str, ...], heading: str, path: Path) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING.fullmatch(line)
        if match is not None and match.group(2) == heading:
            matches.append((index, len(match.group(1))))
    if not matches:
        raise BriefSourceError(
            BriefSourceErrorCode.SELECTOR_INVALID,
            f"Heading '{heading}' is not in '{path}'.",
        )
    if len(matches) != 1:
        raise BriefSourceError(
            BriefSourceErrorCode.SELECTOR_INVALID,
            f"Heading '{heading}' is not unique in '{path}'.",
        )
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = MARKDOWN_HEADING.fullmatch(lines[index])
        if match is not None and len(match.group(1)) <= level:
            end = index
            break
    return start, end


def select_brief_source(
    source_checkout_root: Path,
    selector: AuthoritySelector,
    *,
    require_utf8: bool,
) -> SelectedBriefSource:
    path = source_checkout_root / Path(*selector.relative_path.parts)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise BriefSourceError(
            BriefSourceErrorCode.SOURCE_UNREADABLE,
            f"Cannot read authority at '{path}': {error}",
        ) from error
    if selector.heading is None:
        if require_utf8:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise BriefSourceError(
                    BriefSourceErrorCode.SOURCE_NOT_UTF8,
                    f"Authority '{path}' is not UTF-8 text.",
                ) from error
        raw_lines = raw.splitlines(keepends=True)
        lines = tuple(BriefSourceLine(index, content) for index, content in enumerate(raw_lines, start=1))
        return SelectedBriefSource(selector, raw, 1 if lines else 0, len(lines), True, lines)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BriefSourceError(
            BriefSourceErrorCode.SOURCE_NOT_UTF8,
            f"Heading-selected authority '{path}' is not UTF-8 text.",
        ) from error
    text_lines = tuple(text.splitlines())
    start, end = _selected_heading(text_lines, selector.heading, path)
    selected_lines = tuple(BriefSourceLine(index + 1, f"{text_lines[index]}\n".encode()) for index in range(start, end))
    return SelectedBriefSource(
        selector,
        b"".join(line.content for line in selected_lines),
        start + 1,
        end,
        False,
        selected_lines,
    )


def _reject_overlaps(selected: tuple[tuple[BriefSourceRequest, SelectedBriefSource], ...]) -> None:
    for index, (left_request, left) in enumerate(selected):
        for right_request, right in selected[index + 1 :]:
            if left.selector.relative_path != right.selector.relative_path:
                continue
            if max(left.start_line, right.start_line) <= min(left.end_line, right.end_line):
                raise BriefSourceError(
                    BriefSourceErrorCode.SELECTOR_OVERLAP,
                    f"Authorities '{left_request.authority_id}' and '{right_request.authority_id}' select "
                    f"overlapping lines in '{left.selector.relative_path}'.",
                )


def _segment(
    request: BriefSourceRequest,
    index: int,
    lines: tuple[BriefSourceLine, ...],
) -> BriefSourceSegment:
    content = b"".join(line.content for line in lines)
    return BriefSourceSegment(
        request.authority_id,
        request.selector,
        index,
        lines[0].number if lines else 0,
        lines[-1].number if lines else 0,
        content,
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _segments(
    request: BriefSourceRequest,
    selected: SelectedBriefSource,
    max_batch_bytes: int,
) -> tuple[BriefSourceSegment, ...]:
    if not selected.lines:
        return (_segment(request, 0, ()),)
    segments: list[BriefSourceSegment] = []
    current: list[BriefSourceLine] = []
    current_bytes = 0
    for line in selected.lines:
        line_bytes = len(line.content)
        if line_bytes > max_batch_bytes:
            raise BriefSourceError(
                BriefSourceErrorCode.LINE_TOO_LARGE,
                f"Line {line.number} selected by '{request.authority_id}' is {line_bytes} bytes; "
                f"the limit is {max_batch_bytes}.",
            )
        if current and current_bytes + line_bytes > max_batch_bytes:
            segments.append(_segment(request, len(segments), tuple(current)))
            current = []
            current_bytes = 0
        current.append(line)
        current_bytes += line_bytes
    if current:
        segments.append(_segment(request, len(segments), tuple(current)))
    return tuple(segments)


def _render_segments(segments: tuple[BriefSourceSegment, ...]) -> bytes:
    rendered: list[bytes] = []
    for segment in segments:
        rendered.append(
            (
                f"===== BEGIN BRIEF SOURCE authority={segment.authority_id} selector={segment.selector} "
                f"lines={segment.start_line}-{segment.end_line} segment={segment.index} =====\n"
            ).encode()
        )
        rendered.append(segment.content)
        if segment.content and not segment.content.endswith(b"\n"):
            rendered.append(b"\n")
        rendered.append(
            f"===== END BRIEF SOURCE authority={segment.authority_id} segment={segment.index} =====\n".encode()
        )
    return b"".join(rendered)


def _batch(index: int, segments: tuple[BriefSourceSegment, ...]) -> BriefSourceBatch:
    return BriefSourceBatch(
        index,
        sum(segment.content_byte_count for segment in segments),
        len(_render_segments(segments)),
        segments,
    )


def _batches(segments: tuple[BriefSourceSegment, ...], max_batch_bytes: int) -> tuple[BriefSourceBatch, ...]:
    batches: list[BriefSourceBatch] = []
    current: list[BriefSourceSegment] = []
    current_bytes = 0
    for segment in segments:
        if current and current_bytes + segment.content_byte_count > max_batch_bytes:
            batches.append(_batch(len(batches), tuple(current)))
            current = []
            current_bytes = 0
        current.append(segment)
        current_bytes += segment.content_byte_count
    if current:
        batches.append(_batch(len(batches), tuple(current)))
    return tuple(batches)


def plan_brief_sources(
    source_checkout_root: Path,
    manifest: BriefSourceManifest,
    max_batch_bytes: int,
) -> BriefSourcePlan:
    if max_batch_bytes < 1:
        raise BriefSourceError(BriefSourceErrorCode.MANIFEST_INVALID, "The maximum batch size must be positive.")
    selected = tuple(
        (
            request,
            select_brief_source(
                source_checkout_root,
                parse_authority_selector(request.selector),
                require_utf8=True,
            ),
        )
        for request in manifest.sources
    )
    _reject_overlaps(selected)
    planned_sources: list[PlannedBriefSource] = []
    all_segments: list[BriefSourceSegment] = []
    for request, authority in selected:
        segments = _segments(request, authority, max_batch_bytes)
        all_segments.extend(segments)
        planned_sources.append(
            PlannedBriefSource(
                request.authority_id,
                request.selector,
                request.families,
                hashlib.sha256(authority.content).hexdigest(),
                len(authority.content),
                authority.start_line,
                authority.end_line,
                authority.whole_file,
                segments,
            )
        )
    canonical_manifest = msgspec.json.encode(manifest, order="sorted")
    return BriefSourcePlan(
        "pinboard-brief-source-plan/v1",
        hashlib.sha256(canonical_manifest).hexdigest(),
        max_batch_bytes,
        tuple(planned_sources),
        _batches(tuple(all_segments), max_batch_bytes),
    )


def render_brief_source_batch(plan: BriefSourcePlan, batch_index: int) -> bytes:
    if batch_index < 0 or batch_index >= len(plan.batches):
        raise BriefSourceError(
            BriefSourceErrorCode.BATCH_NOT_FOUND,
            f"Batch {batch_index} is outside the available range 0..{len(plan.batches) - 1}.",
        )
    return _render_segments(plan.batches[batch_index].segments)
