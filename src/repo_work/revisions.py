import hashlib
from pathlib import Path

from repo_work.markdown import parse_item


def _relative(path: Path, work_root: Path) -> str:
    return str(path.relative_to(work_root))


def subject_revision(work_root: Path, item_id: str) -> str:
    item_path = work_root / "items" / f"{item_id}.md"
    record = parse_item(item_path)
    item = record.queue_item
    paths: list[Path] = [item_path]
    dependencies = () if item is None else item.depends_on
    for dependency in dependencies:
        live = work_root / "items" / f"{dependency}.md"
        history = work_root / "history" / "items" / f"{dependency}.md"
        paths.append(live if live.is_file() else history)
    if item is not None and item.attempt is not None:
        paths.append(work_root / "attempts" / item.attempt / "attempt.md")
    for resource in record.resources:
        paths.append(work_root / "resources" / f"{resource}.md")
        claim_root = work_root / "leases" / "resources"
        paths.extend(sorted(claim_root.glob(f"{resource}--*.md")) if claim_root.is_dir() else ())
    digest = hashlib.sha256()

    def sort_key(candidate: Path) -> str:
        return _relative(candidate, work_root)

    for path in sorted(set(paths), key=sort_key):
        relative = _relative(path, work_root).encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_file():
            data = path.read_bytes()
            digest.update(b"F")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        else:
            digest.update(b"M")
    return digest.hexdigest()


def proposal_revision(work_root: Path, proposal_id: str) -> str:
    return hashlib.sha256((work_root / "inbox" / f"{proposal_id}.json").read_bytes()).hexdigest()
