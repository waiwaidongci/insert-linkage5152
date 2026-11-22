import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain import analysis, revisions
from src.domain.analysis import analyze, build_result
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset, DatasetRevision
from src.services.audit import record
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                latest_dataset = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1))
                if latest_dataset:
                    dataset_id = latest_dataset
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            revision = db.scalar(select(DatasetRevision.revision)
                                 .where(DatasetRevision.dataset_id == dataset_id)
                                 .order_by(DatasetRevision.revision.desc()).limit(1))
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author,
                       revision=revision))


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        if job.revision is None:
            records = db.get(Dataset, job.dataset_id).rows
        else:
            records = db.get(DatasetRevision, (job.dataset_id, job.revision)).rows
        return job.id, job.attempt, job.revision, records, job.params


def _run_analysis(db, job):
    """从已提交的规格对距离执行切树；规格对检查点由 execute 分事务先行落库。"""
    if job.revision is None:
        return analyze(db.get(Dataset, job.dataset_id).rows, job.params)
    revision = db.get(DatasetRevision, (job.dataset_id, job.revision))
    _records, ids, names, traits = analysis.prepare_records(revision.rows)
    cut = analysis._cut_distance(job.params)
    matrix = revisions.load_matrix(db, job, ids)
    # 优先用检查点事务记录的真实重算量（中断恢复时只统计补算部分）。
    progress = job.pair_progress or revisions.revision_progress(db, job, ids)
    return build_result(ids, names, traits, matrix, cut, progress)


def execute(lease):
    job_id, attempt, revision, _rows, _params = lease
    result = export = failure = None
    try:
        if revision is None:
            with Session.begin() as db:
                job = db.get(Run, job_id, with_for_update=True)
                result = _run_analysis(db, job)
        else:
            # 检查点事务：补齐并提交本版规格对距离。进程在此之后硬中断，
            # 已算完的规格对已落库，恢复时 ensure_pair_distances 会跳过它们。
            with Session.begin() as db:
                job = db.get(Run, job_id, with_for_update=True)
                _ids, _traits, progress = revisions.ensure_pair_distances(db, job)
                if progress is not None and job.pair_progress is None:
                    # 记录本次（含恢复后补算）实际重算/沿用的规格对数。
                    job.pair_progress = progress
            # 切树事务：从已提交的规格对距离构建结果，不再改动任何规格对。
            with Session.begin() as db:
                job = db.get(Run, job_id, with_for_update=True)
                result = _run_analysis(db, job)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        event = f'analysis.{job.status}'
        if not failure and job.revision is not None:
            published, active_revision = revisions.publish(db, job)
            if not published:
                # 晚完成的旧版不能顶掉已发布的新版：只标记被取代，不移动发布指针。
                job.superseded = True
                event = 'analysis.superseded'
                job.result = {**result, 'note': (
                    result['note']
                    + f' 第 {job.revision} 版完成时第 {active_revision} 版已发布，本版未上线。'
                )}
                result = job.result
        record(db, 'worker', event, job.id, attempt=attempt,
               revision=job.revision, reason=failure)
        if job.schedule_id and not job.superseded:
            plan = db.get(Schedule, job.schedule_id)
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient, state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
