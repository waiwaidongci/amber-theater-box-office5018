import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze
from src.domain.spec import SPEC
from src.models.jobs import Delivery, ReportPublication, Run, Schedule
from src.models.source import Dataset
from src.services.audit import record
from src.services.publication import idem_key, publish_run
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
                period_key = SPEC.get('schedule_date_param')
                next_params = dict(item.params)
                if period_key:
                    report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                    params[period_key] = report_day.isoformat()
                    # 每日计划始终取最新快照批次，晚到退款补录后的新批次会被纳入。
                    dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                    # 仅在运行成功落库后才推进计划参数，避免失败重放时丢营业日。
                    next_params[period_key] = (report_day + timedelta(days=1)).isoformat()
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            exists = db.scalar(select(Run.id).where(Run.schedule_id == item.id, Run.scheduled_at == due))
            if exists:
                record(db, 'worker', 'analysis.skipped_duplicate', exists, schedule_id=item.id)
                continue
            dataset = db.get(Dataset, dataset_id)
            key = idem_key(params.get(SPEC.get('schedule_date_param', 'report_day')),
                           dataset.checksum, params) if dataset else None
            if key and db.scalar(select(Run.id).where(Run.idem_key == key)):
                record(db, 'worker', 'analysis.skipped_duplicate', key, schedule_id=item.id)
                continue
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author, idem_key=key))
            if item.repeat_daily and period_key:
                item.params = next_params


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
        records = db.get(Dataset, job.dataset_id).rows
        return job.id, job.attempt, records, job.params


def execute(lease):
    job_id, attempt, rows, params = lease
    result = export = failure = None
    try:
        result = analyze(rows, params)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        # 租约易主（另一 worker 已接手）或状态已终结时，本次结果作废，不重复落库。
        if job.attempt != attempt or job.status != Status.RUNNING:
            log.warning('Discard stale worker result for run %s attempt %s', job_id, attempt)
            return
        job.lease_until = None
        job.finished_at = now()
        if failure:
            job.status = Status.FAILED
            job.result = None
            job.error = failure
            record(db, 'worker', 'analysis.failed', job.id, attempt=attempt, reason=failure)
            return

        job.status = Status.COMPLETE
        job.result = result
        job.export_file = export
        job.error = None
        publication, publish_error = publish_run(db, job, result, export)
        record(db, 'worker', 'analysis.complete', job.id, attempt=attempt,
               publication_id=publication.id if publication else None, reason=publish_error)
        if publish_error:
            # 底稿与桥接不一致时不切换版本：页面沿用旧版，本次运行标记失败并保留排查线索。
            job.status = Status.FAILED
            job.error = publish_error[:500]
            record(db, 'worker', 'publication.rejected', job.id, reason=publish_error)
            return
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            ready = result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else result['headline']
            # 同运行只允许一个回执；重跑/恢复不重复生成，保留既有签收记录。
            existing = db.scalar(select(Delivery).where(Delivery.run_id == job.id))
            if existing is None:
                receipt = Delivery(run_id=job.id, recipient=plan.recipient,
                                   state='delivered' if ready else 'blocked', detail=detail[:500])
                db.add(receipt)
                db.flush()
                record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
