import hashlib
import json
import logging

from sqlalchemy import select

from src.constants.enums import PublicationState
from src.models.jobs import ReportPublication
from src.models.source import Dataset
from src.services.analysis import result_fingerprint
from src.utils.time import now

log = logging.getLogger(__name__)


def idem_key(report_day, dataset_checksum, params):
    """同一营业日、同一台账快照与同一计算口径共享幂等键：
    重跑、同批重跑与并发提交只会落到同一条运行上。"""
    payload = json.dumps({'day': report_day, 'dataset': dataset_checksum, 'params': params},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:100]


def verify_artifact(export_path, result):
    """发布前门控：磁盘上的 CSV 底稿必须仍然存在，且其桥接、渠道处置、
    复算底稿与运行结果同源（重新计算结果指纹并比对发布指纹）。"""
    if not export_path.is_file():
        return False, 'CSV 底稿缺失，拒绝切换版本'
    try:
        current = result_fingerprint(result)
    except (TypeError, ValueError):
        return False, '结果载荷无法生成校验指纹'
    if current != result.get('fingerprint'):
        return False, '桥接结果与底稿校验指纹不一致，拒绝切换版本'
    return True, None


def publish_run(db, job, result, export_name):
    """在单个事务内完成版本切换：
    1. 旧版本保持 active，直到新版本全部就绪；
    2. 校验整份桥接、渠道处置与 CSV 底稿一致；
    3. 一致后才把旧版一次性标记 superseded 并激活新版。
    较早完成的旧任务（运行 id 更小）不得覆盖更新版。"""
    from src.config.settings import ARTIFACTS

    day = result['snapshot']['report_day']
    dataset = db.get(Dataset, job.dataset_id)
    ok, reason = verify_artifact(ARTIFACTS / export_name, result)
    if not ok:
        latest_version = db.scalar(
            select(ReportPublication.version).where(ReportPublication.report_day == day)
            .order_by(ReportPublication.version.desc()).limit(1)) or 0
        rejected = ReportPublication(
            report_day=day, version=latest_version + 1, run_id=job.id, dataset_id=job.dataset_id,
            dataset_checksum=dataset.checksum, fingerprint=result.get('fingerprint', ''),
            export_file=export_name, state=PublicationState.REJECTED)
        db.add(rejected)
        db.flush()
        job.publication_id = None
        return None, reason

    stamp = now()
    active = db.scalar(select(ReportPublication)
                       .where(ReportPublication.report_day == day,
                              ReportPublication.state == PublicationState.ACTIVE)
                       .with_for_update())
    # 同指纹重跑（例如断点恢复后结果未变）不产生新版本，沿用已发布版本与已签收日报。
    if active and active.fingerprint == result['fingerprint'] and active.dataset_checksum == dataset.checksum:
        job.publication_id = active.id
        return active, None

    # 并发保护：已有更新的运行（更大 run id）发布过版本时，旧任务不得覆盖。
    if active and active.run_id > job.id:
        log.warning('Run %s is older than published run %s for %s; keeping current version',
                    job.id, active.run_id, day)
        return None, '已有更新版本发布，旧运行结果已忽略'

    latest_version = db.scalar(
        select(ReportPublication.version).where(ReportPublication.report_day == day)
        .order_by(ReportPublication.version.desc()).limit(1)) or 0
    publication = ReportPublication(
        report_day=day, version=latest_version + 1, run_id=job.id, dataset_id=job.dataset_id,
        dataset_checksum=dataset.checksum, fingerprint=result['fingerprint'],
        export_file=export_name, state=PublicationState.ACTIVE, published_at=stamp)
    db.add(publication)
    db.flush()
    if active:
        active.state = PublicationState.SUPERSEDED
        active.superseded_at = stamp
    job.publication_id = publication.id
    return publication, None


def active_publication(db, report_day):
    return db.scalar(select(ReportPublication)
                     .where(ReportPublication.report_day == report_day,
                            ReportPublication.state == PublicationState.ACTIVE)
                     .order_by(ReportPublication.version.desc()).limit(1))
