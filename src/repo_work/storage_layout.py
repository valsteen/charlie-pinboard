from pathlib import Path


def lock_path_for(work_root: Path) -> Path:
    return work_root.parent / f".{work_root.name}.repo-work.lock"


def journal_path_for(work_root: Path) -> Path:
    return work_root.parent / f".{work_root.name}.repo-work-journal"
