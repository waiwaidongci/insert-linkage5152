"""内衬批次更正：在同一业务批次上逐版登记修正后的适配特征。

每版只重算至少一端较前版有改动的规格对，未受影响的规格对沿用上一版距离，
因此切树结果与整批重算完全一致。每版冻结自己的特征快照，多版可并发重算；
发布以版本号排序，晚完成的旧版不会顶掉已发布的新版。
"""

from array import array

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import analysis
from ..models.jobs import Run
from ..models.source import DatasetRevision, PairDistance, PublishedRevision
from ..services.datasets import checksum


def _revision_index(db, dataset_id):
    return db.scalar(select(DatasetRevision.revision).where(DatasetRevision.dataset_id == dataset_id)
                     .order_by(DatasetRevision.revision.desc()).limit(1)) or 0


def register_revision(db, dataset, rows, actor):
    """登记下一版更正，前版未算完也可以登记；同内容重复提交复用该版。"""
    normalized = analysis.validate_rows(rows)
    signature = checksum(normalized)
    lookup = select(DatasetRevision).where(DatasetRevision.dataset_id == dataset.id,
                                           DatasetRevision.checksum == signature)
    existing = db.scalar(lookup)
    if existing:
        return existing, False
    index = _revision_index(db, dataset.id) + 1
    revision = DatasetRevision(dataset_id=dataset.id, revision=index, checksum=signature,
                               rows=normalized, row_count=len(normalized), author=actor)
    try:
        with db.begin_nested():
            db.add(revision)
            db.flush()
    except IntegrityError:
        existing = db.scalar(lookup.with_for_update())
        if existing:
            return existing, False
        raise
    return revision, True


def _spec_index(records):
    keys = sorted(row["spec_id"].casefold() for row in records)
    return {key: index for index, key in enumerate(keys)}


def affected_pairs(previous, current):
    """返回至少一端较前版有改动的规格对下标集合，键为 (left, right) 且 left<right。

    下标按规格编号折叠大小写排序后的位置确定，前后版共用同一编号空间。
    """
    before = {row["spec_id"].casefold(): row for row in previous}
    after = {row["spec_id"].casefold(): row for row in current}
    changed_keys = {
        key for key in before.keys() & after.keys()
        if before[key]["fit_traits"] != after[key]["fit_traits"]
    }
    changed_keys |= after.keys() - before.keys()  # 新增规格的全部配对都要重算
    ordered = sorted(after, key=lambda key: key.casefold())
    changed = {index for index, key in enumerate(ordered) if key in changed_keys}
    pairs = set()
    for right in range(len(current)):
        for left in range(right):
            if left in changed or right in changed:
                pairs.add((left, right))
    return changed, pairs


def _stored_pairs(db, dataset_id, revision):
    return {
        (item.left_index, item.right_index): item.distance
        for item in db.scalars(select(PairDistance).where(
            PairDistance.dataset_id == dataset_id, PairDistance.revision == revision))
    }


def _recompute_plan(db, run, ids, traits, current_stored):
    """决定每个规格对是本版重算还是沿用上一版，并填充下三角矩阵。

    返回矩阵与本版的重算/沿用计数。current_stored 中已存在的规格对是本版
    检查点已算完的，直接沿用、不计入本次重算。
    """
    matrix = [array("d", [0.0]) * index for index in range(len(ids))]
    if run.revision == 1:
        recomputed = 0
        for right, distances in enumerate(matrix):
            for left in range(len(distances)):
                if (left, right) in current_stored:
                    distances[left] = current_stored[(left, right)]
                else:
                    distances[left] = analysis.pair_distance(traits, left, right)
                    recomputed += 1
        return matrix, recomputed, 0
    previous = db.get(DatasetRevision, (run.dataset_id, run.revision - 1)).rows
    current = db.get(DatasetRevision, (run.dataset_id, run.revision)).rows
    before_index = _spec_index(previous)
    _changed, targets = affected_pairs(previous, current)
    cached = _stored_pairs(db, run.dataset_id, run.revision - 1)
    recomputed = reused = 0
    for right in range(len(ids)):
        for left in range(right):
            key = (left, right)
            if key in current_stored:
                matrix[right][left] = current_stored[key]
                continue
            left_key, right_key = ids[left].casefold(), ids[right].casefold()
            cache_key = (
                (before_index[left_key], before_index[right_key])
                if left_key in before_index and right_key in before_index else None
            )
            if cache_key is None or key in targets or cache_key not in cached:
                matrix[right][left] = analysis.pair_distance(traits, left, right)
                recomputed += 1
            else:
                matrix[right][left] = cached[cache_key]
                reused += 1
    return matrix, recomputed, reused


def ensure_pair_distances(db, run):
    """在检查点事务内补齐并持久化本版规格对距离，供中断恢复与下一版沿用。

    已存在的规格对（上一检查点算完）直接沿用，不再重算。调用方应在独立事务中
    提交，使进程硬中断后已算完的规格对仍然保留。
    """
    revision = db.get(DatasetRevision, (run.dataset_id, run.revision))
    _records, ids, _names, traits = analysis.prepare_records(revision.rows)
    stored = _stored_pairs(db, run.dataset_id, run.revision)
    matrix, recomputed, reused = _recompute_plan(db, run, ids, traits, stored)
    for right, distances in enumerate(matrix):
        for left, height in enumerate(distances):
            if (left, right) not in stored:
                db.add(PairDistance(dataset_id=run.dataset_id, revision=run.revision,
                                    left_index=left, right_index=right, distance=height))
    if run.revision == 1:
        total = len(ids) * (len(ids) - 1) // 2
        progress = None if not stored else {"recomputed": recomputed, "reused": total - recomputed}
    else:
        progress = {"recomputed": recomputed, "reused": reused}
    return ids, traits, progress


def load_matrix(db, run, ids):
    """从已提交的规格对距离重建下三角矩阵，切树与整批重算共用同一条算法链路。"""
    stored = _stored_pairs(db, run.dataset_id, run.revision)
    matrix = [array("d", [0.0]) * index for index in range(len(ids))]
    for (left, right), height in stored.items():
        matrix[right][left] = height
    return matrix


def revision_progress(db, run, ids):
    """本版相对前版的重算/沿用计数（与是否中断恢复无关，描述版本固有工作量）。"""
    if run.revision == 1:
        return None
    previous = db.get(DatasetRevision, (run.dataset_id, run.revision - 1)).rows
    current = db.get(DatasetRevision, (run.dataset_id, run.revision)).rows
    before_index = _spec_index(previous)
    _changed, targets = affected_pairs(previous, current)
    cached = _stored_pairs(db, run.dataset_id, run.revision - 1)
    recomputed = reused = 0
    for right in range(len(ids)):
        for left in range(right):
            left_key, right_key = ids[left].casefold(), ids[right].casefold()
            cache_key = (
                (before_index[left_key], before_index[right_key])
                if left_key in before_index and right_key in before_index else None
            )
            if cache_key is None or (left, right) in targets or cache_key not in cached:
                recomputed += 1
            else:
                reused += 1
    return {"recomputed": recomputed, "reused": reused}


def publish(db, run):
    """发布当前版本；已发布更新版时，晚完成的旧版只标记为被取代。"""
    published = db.scalar(select(PublishedRevision).where(
        PublishedRevision.dataset_id == run.dataset_id).with_for_update())
    if published and published.revision >= run.revision:
        return False, published.revision
    if published:
        previous = db.get(Run, published.run_id)
        if previous:
            previous.superseded = True
        published.revision = run.revision
        published.run_id = run.id
        published.published_at = run.finished_at
    else:
        db.add(PublishedRevision(dataset_id=run.dataset_id, revision=run.revision,
                                 run_id=run.id, published_at=run.finished_at))
    return True, run.revision


def lineage(db, dataset_id):
    """汇总批次版本链，供页面展示各版状态与发布情况。"""
    revisions = db.scalars(select(DatasetRevision).where(
        DatasetRevision.dataset_id == dataset_id).order_by(DatasetRevision.revision)).all()
    published = db.scalar(select(PublishedRevision).where(
        PublishedRevision.dataset_id == dataset_id))
    runs = db.scalars(select(Run).where(Run.dataset_id == dataset_id, Run.revision.is_not(None))
                      .order_by(Run.id)).all()
    run_by_revision = {}
    for run in runs:
        run_by_revision.setdefault(run.revision, []).append(run)
    items = []
    for revision in revisions:
        revision_runs = run_by_revision.get(revision.revision, [])
        items.append({
            "revision": revision.revision,
            "checksum": revision.checksum,
            "row_count": revision.row_count,
            "author": revision.author,
            "created_at": revision.created_at,
            "published": bool(published and published.revision == revision.revision),
            "runs": [{"id": run.id, "status": run.status, "name": run.name} for run in revision_runs],
        })
    return items
