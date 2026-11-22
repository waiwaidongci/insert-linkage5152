from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain import revisions
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.source import Dataset, DatasetRevision, PublishedRevision
from src.schemas.requests import RevisionRequest, Upload
from src.services.audit import record
from src.services.datasets import save, template
from src.services.validation import parse_rows
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter()


@router.get('/spec')
def spec():
    return {key: value for key, value in SPEC.items() if key != 'fixture'}


@router.get('/template')
def sample(actor: User = Depends(current)):
    filename = quote(SPEC['sample_name'])
    return Response(template(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"})


@router.get('/datasets')
def datasets(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.dataset(item) for item in db.scalars(select(Dataset).order_by(Dataset.id.desc()).limit(100))]


@router.get('/datasets/{dataset_id}')
def details(dataset_id: int, offset: int = Query(0, ge=0), limit: int = Query(16, ge=1, le=100),
            db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(Dataset, dataset_id)
    if not item:
        raise HTTPException(404, '找不到该数据批次')
    return {**views.dataset(item), 'rows': item.rows[offset:offset + limit]}


@router.post('/datasets', status_code=201)
def upload(body: Upload, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    rows = parse_rows(body.csv_text)
    item, created = save(db, body.name, body.filename, rows, actor.username)
    if created:
        record(db, actor.username, 'dataset.imported', item.id, rows=item.row_count)
    db.commit()
    return {**views.dataset(item), 'created': created}


@router.get('/datasets/{dataset_id}/revisions')
def revision_lineage(dataset_id: int, db: Session = Depends(connection),
                     actor: User = Depends(current)):
    if not db.get(Dataset, dataset_id):
        raise HTTPException(404, '找不到该数据批次')
    return revisions.lineage(db, dataset_id)


@router.get('/datasets/{dataset_id}/revisions/{revision}')
def revision_detail(dataset_id: int, revision: int, offset: int = Query(0, ge=0),
                    limit: int = Query(16, ge=1, le=100),
                    db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(DatasetRevision, (dataset_id, revision))
    if not item:
        raise HTTPException(404, '找不到该更正版本')
    published = db.get(PublishedRevision, dataset_id)
    return {
        'dataset_id': dataset_id, 'revision': item.revision, 'checksum': item.checksum,
        'row_count': item.row_count, 'author': item.author,
        'published': bool(published and published.revision == item.revision),
        'created_at': views.iso(item.created_at), 'rows': item.rows[offset:offset + limit],
    }


@router.post('/datasets/{dataset_id}/revisions', status_code=201)
def register(dataset_id: int, body: RevisionRequest, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到该数据批次')
    rows = parse_rows(body.csv_text)
    item, created = revisions.register_revision(db, dataset, rows, actor.username)
    if created:
        record(db, actor.username, 'revision.registered', f'{dataset_id}#{item.revision}',
               revision=item.revision, rows=item.row_count, filename=body.filename)
    db.commit()
    wake()
    return {
        'dataset_id': dataset_id, 'revision': item.revision, 'created': created,
        'row_count': item.row_count, 'checksum': item.checksum,
        'created_at': views.iso(item.created_at),
    }
