from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Review, Role, Status
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import ReportPublication, Run
from src.models.source import Dataset
from src.schemas.requests import ReviewRequest, RunRequest
from src.services.audit import record
from src.services.publication import active_publication, idem_key
from src.services.validation import normalize_params
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter(prefix='/runs')


def require_job(db, run_id, lock=False):
    item = db.get(Run, run_id, with_for_update=lock)
    if not item:
        raise HTTPException(404, '找不到该运行记录')
    return item


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.run(item) for item in db.scalars(select(Run).order_by(Run.id.desc()).limit(100))]


@router.post('', status_code=202)
def create(body: RunRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    dataset = db.get(Dataset, body.dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到指定的数据批次')
    params = normalize_params(body.params)
    key = idem_key(params.get('report_day'), dataset.checksum, params)
    # 同批重跑/并发提交：复用既有运行，不重复生成结果。排队或执行中的请求直接返回同一条。
    existing = db.scalar(select(Run).where(Run.idem_key == key).with_for_update(skip_locked=True))
    if existing is not None:
        record(db, actor.username, 'analysis.deduplicated', existing.id, idem_key=key)
        db.commit()
        return views.run(existing, detail=True)
    item = Run(name=body.name, dataset_id=body.dataset_id, params=params,
               author=actor.username, idem_key=key)
    try:
        db.add(item)
        db.flush()
    except IntegrityError:
        # 两个并发请求同时通过上面的 SELECT：唯一约束兜底，回滚后返回已存在的运行。
        db.rollback()
        existing = db.scalar(select(Run).where(Run.idem_key == key))
        record(db, actor.username, 'analysis.deduplicated', existing.id, idem_key=key)
        db.commit()
        return views.run(existing, detail=True)
    record(db, actor.username, 'analysis.queued', item.id, dataset_id=item.dataset_id, idem_key=key)
    db.commit()
    wake()
    return views.run(item, detail=True)


@router.get('/published/{report_day}')
def published(report_day: str, db: Session = Depends(connection), actor: User = Depends(current)):
    """重算期间对外只暴露当前生效版本；尚未发布过的营业日返回 404。"""
    publication = active_publication(db, report_day)
    if publication is None:
        raise HTTPException(404, '该营业日尚无已发布日报')
    job = require_job(db, publication.run_id)
    payload = views.run(job, detail=True)
    payload['publication'] = views.publication(publication)
    return payload


@router.get('/{run_id}')
def detail(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = require_job(db, run_id)
    payload = views.run(item, detail=True)
    if item.publication_id:
        payload['publication'] = views.publication(db.get(ReportPublication, item.publication_id))
    return payload


@router.get('/{run_id}/export')
def export(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = require_job(db, run_id)
    # 下载始终走已发布版本的底稿，保证页面桥接与 CSV 一致；历史版本的文件仍保留可查。
    publication = db.get(ReportPublication, item.publication_id) if item.publication_id else None
    if publication is None:
        raise HTTPException(409, '此记录未通过一致性发布，暂无可下载结果')
    path = ARTIFACTS / publication.export_file
    if not path.is_file():
        raise HTTPException(410, '导出文件已不可用，请重新运行')
    return FileResponse(path, media_type='text/csv',
                        filename=f'{SPEC["short_name"]}-{item.params.get("report_day", "day")}-v{publication.version}.csv')


@router.post('/{run_id}/review')
def review(run_id: int, body: ReviewRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN))):
    item = require_job(db, run_id, lock=True)
    if item.status != Status.COMPLETE or item.review != Review.PENDING:
        raise HTTPException(409, '只有已完成且待复核的记录可以提交决定')
    if body.review == Review.PENDING:
        raise ValueError('请选择通过或退回')
    if body.review == Review.ACCEPTED and not body.decision:
        raise ValueError('通过复核时请填写处置决定')
    modes = SPEC.get('modes')
    if modes and body.decision and body.decision not in modes:
        raise ValueError('请选择项目支持的处置决定')
    item.review, item.review_note, item.decision = body.review, body.note, body.decision or None
    record(db, actor.username, f'review.{item.review}', item.id, note=body.note, decision=item.decision)
    db.commit()
    return views.run(item, detail=True)
