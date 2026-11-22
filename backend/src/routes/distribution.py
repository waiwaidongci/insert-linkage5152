from datetime import UTC, timedelta
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset, PublishedRevision
from src.schemas.requests import ScheduleRequest
from src.services.audit import record
from src.services.validation import normalize_params
from src.utils import views
from src.utils.time import now
from src.utils.wakeup import wake

router = APIRouter()


@router.get('/schedules')
def schedules(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.schedule(item) for item in db.scalars(select(Schedule).order_by(Schedule.id.desc()).limit(100))]


@router.post('/schedules', status_code=201)
def add(body: ScheduleRequest, db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    if body.first_at.utcoffset() is None:
        raise ValueError('首次运行时间需要明确时区')
    first = body.first_at.astimezone(UTC).replace(tzinfo=None)
    stamp = now()
    if first < stamp - timedelta(seconds=30) or first > stamp + timedelta(days=366):
        raise ValueError('请选择从现在起一年内的首次运行时间')
    item = Schedule(dataset_id=body.dataset_id, params=normalize_params(body.params), name=body.name,
                    recipient=body.recipient, next_at=first, repeat_daily=body.repeat_daily, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'schedule.created', item.id, recipient=item.recipient)
    db.commit()
    wake()
    return views.schedule(item)


@router.post('/schedules/{schedule_id}/disable')
def disable(schedule_id: int, db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    item = db.get(Schedule, schedule_id, with_for_update=True)
    if not item:
        raise HTTPException(404, '找不到该分发计划')
    if item.enabled:
        item.enabled = False
        record(db, actor.username, 'schedule.disabled', item.id)
        db.commit()
    return views.schedule(item)


@router.get('/deliveries')
def outbox(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.delivery(item) for item in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]


@router.get('/datasets/{dataset_id}/latest-export')
def latest_export(dataset_id: int, db: Session = Depends(connection),
                  actor: User = Depends(current)):
    """交付页固定取已发布的最新完整版本，原签核文件仍从各运行记录下载。"""
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到该数据批次')
    pointer = db.get(PublishedRevision, dataset_id)
    if not pointer:
        raise HTTPException(409, '该批次还没有已发布的更正版本')
    run = db.get(Run, pointer.run_id)
    if not run or not run.export_file:
        raise HTTPException(410, '最新版本的导出文件已不可用，请重新运行')
    path = ARTIFACTS / run.export_file
    if not path.is_file():
        raise HTTPException(410, '导出文件已不可用，请重新运行')
    record(db, actor.username, 'delivery.latest_download', run.id,
           revision=pointer.revision)
    db.commit()
    filename = quote(f'{SPEC["short_name"]}-{dataset_id}-r{pointer.revision}.csv')
    return FileResponse(path, media_type='text/csv',
                        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"})
