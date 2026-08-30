"""Plan or emit deterministic reviewed-authority source batches.

This command owner reads the selected manifest and writes only its requested
stdout representation. Source selection remains in ``brief_sources``; this
module owns the exact presentation conversion and command branching.
"""

import sys
from typing import assert_never

from pinboard.interfaces import brief_source_models, brief_sources, cli_commands
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import BriefSourceError, BriefSourceErrorCode


def _brief_source_segment_view(
    segment: brief_source_models.BriefSourceSegment,
) -> brief_source_models.BriefSourceSegmentView:
    return brief_source_models.BriefSourceSegmentView(
        segment.authority_id,
        segment.selector,
        segment.index,
        segment.start_line,
        segment.end_line,
        segment.content_byte_count,
        segment.content_sha256,
    )


def _brief_source_view(source: brief_source_models.PlannedBriefSource) -> brief_source_models.BriefSourceView:
    return brief_source_models.BriefSourceView(
        source.authority_id,
        source.selector,
        source.families,
        source.selected_sha256,
        source.selected_byte_count,
        source.start_line,
        source.end_line,
        source.whole_file,
        tuple(_brief_source_segment_view(segment) for segment in source.segments),
    )


def _brief_source_batch_view(batch: brief_source_models.BriefSourceBatch) -> brief_source_models.BriefSourceBatchView:
    return brief_source_models.BriefSourceBatchView(
        batch.index,
        batch.content_byte_count,
        batch.estimated_rendered_byte_count,
        tuple(_brief_source_segment_view(segment) for segment in batch.segments),
    )


def _brief_source_plan_view(plan: brief_source_models.BriefSourcePlan) -> brief_source_models.BriefSourcePlanView:
    return brief_source_models.BriefSourcePlanView(
        plan.schema,
        plan.manifest_sha256,
        plan.max_batch_bytes,
        tuple(_brief_source_view(source) for source in plan.sources),
        tuple(_brief_source_batch_view(batch) for batch in plan.batches),
    )


def run_brief_sources(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.BriefSourcesPlanCommand | cli_commands.BriefSourcesEmitCommand,
) -> int:
    try:
        raw_manifest = command.file.read_bytes()
    except OSError as error:
        raise BriefSourceError(
            BriefSourceErrorCode.MANIFEST_INVALID,
            f"Cannot read brief source manifest '{command.file}': {error}",
        ) from error
    plan = brief_sources.plan_brief_sources(
        roots.source_checkout,
        brief_sources.decode_brief_source_manifest(raw_manifest),
        command.max_batch_bytes,
    )
    match command:
        case cli_commands.BriefSourcesPlanCommand():
            write_json(_brief_source_plan_view(plan))
        case cli_commands.BriefSourcesEmitCommand(emit_batch=batch_index):
            sys.stdout.write(brief_sources.render_brief_source_batch(plan, batch_index).decode("utf-8"))
        case _ as unreachable:
            assert_never(unreachable)
    return 0
