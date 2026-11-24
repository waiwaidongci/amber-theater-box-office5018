"""版本发布与幂等重算的端到端测试。

使用 SQLite 文件库（行锁子句在 SQLite 上被忽略，足以验证流程语义），
不依赖 Redis、PostgreSQL 或网络。运行：

    cd backend && python3 -m unittest tests.test_publication_flow -v
"""
import copy
import csv
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='hxwl-test-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP}/test.db')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', f'{_TMP}/artifacts')
os.environ.setdefault('SEED_DEMO', 'false')

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.database import Base, Session, engine
from src.config.migrations import run_migrations
from src.config.settings import ARTIFACTS
from src.constants.enums import PublicationState, Review, Status
from src.domain.analysis import analyze
from src.domain.demo import demo_rows
from src.models.jobs import Delivery, ReportPublication, Run, Schedule
from src.models.source import Dataset
from src.services import execution
from src.services.datasets import save
from src.services.publication import idem_key, publish_run
from src.services.validation import normalize_params
from src.utils.exports import write_report
from src.utils.time import now

DAY = '2026-11-23'


class PublicationFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        run_migrations()
        cls.base_rows = demo_rows()
        cls.params = normalize_params({'report_day': DAY})
        # 晚到退款：对该营业日第一行追加 500 元实退，仍按原营业日/渠道/票档归集。
        cls.late_rows = copy.deepcopy(cls.base_rows)
        target = next(row for row in cls.late_rows if row['report_day'] == DAY)
        target['actual_refund_cents'] += 50_000

    def setUp(self):
        # 每个用例使用全新表，避免运行与发布版本在共享文件库中相互污染。
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        run_migrations()
        with Session.begin() as db:
            ds1, _ = save(db, '基础快照', 'base.csv', self.base_rows, 'analyst')
            ds2, _ = save(db, '含晚到退款快照', 'late.csv', self.late_rows, 'analyst')
            self.ds1_id, self.ds2_id = ds1.id, ds2.id

    def _enqueue(self, dataset_id, params=None, schedule_id=None, scheduled_at=None):
        with Session.begin() as db:
            dataset = db.get(Dataset, dataset_id)
            params = params or self.params
            key = idem_key(params['report_day'], dataset.checksum, params)
            run = Run(dataset_id=dataset_id, schedule_id=schedule_id, scheduled_at=scheduled_at,
                      name=f'运行-{dataset_id}', params=dict(params), author='analyst', idem_key=key)
            db.add(run)
            db.flush()
            return run.id

    def _run_to_completion(self, run_id):
        lease = execution.claim()
        self.assertIsNotNone(lease)
        self.assertEqual(lease[0], run_id)
        execution.execute(lease)
        with Session() as db:
            return db.get(Run, run_id)

    def test_01_late_refund_uses_original_day_and_new_snapshot(self):
        r1 = self._enqueue(self.ds1_id)
        job1 = self._run_to_completion(r1)
        self.assertEqual(job1.status, Status.COMPLETE)
        net_before = job1.result['metrics'][3]['value']
        fp_snapshot_1 = job1.result['snapshot']['ledger_fingerprint']

        r2 = self._enqueue(self.ds2_id)
        job2 = self._run_to_completion(r2)
        self.assertEqual(job2.result['snapshot']['report_day'], DAY)
        self.assertNotEqual(job2.result['snapshot']['ledger_fingerprint'], fp_snapshot_1)
        # 晚到退款 500 元，净实收应等额下降。
        self.assertAlmostEqual(net_before - job2.result['metrics'][3]['value'], 500.0, places=2)

        with Session() as db:
            pubs = db.scalars(select(ReportPublication).where(ReportPublication.report_day == DAY)
                              .order_by(ReportPublication.version)).all()
            self.assertEqual([p.version for p in pubs], [1, 2])
            self.assertEqual(pubs[0].state, PublicationState.SUPERSEDED)
            self.assertEqual(pubs[1].state, PublicationState.ACTIVE)
            self.assertEqual(pubs[1].run_id, r2)
            # 旧版运行结果与文件仍然保留。
            self.assertIsNotNone(db.get(Run, r1).result)
            self.assertTrue((ARTIFACTS / pubs[0].export_file).is_file())

    def test_02_manual_rerun_is_idempotent(self):
        key = idem_key(DAY, self._checksum(self.ds1_id), self.params)
        r1 = self._enqueue(self.ds1_id)
        with Session.begin() as db:
            duplicate = Run(dataset_id=self.ds1_id, name='重复运行', params=dict(self.params),
                            author='analyst', idem_key=key)
            db.add(duplicate)
            with self.assertRaises(IntegrityError):
                db.flush()
        with Session() as db:
            self.assertEqual(db.scalar(select(Run).where(Run.idem_key == key)),
                             db.get(Run, r1))

    def test_03_resume_after_interruption_does_not_duplicate(self):
        r1 = self._enqueue(self.ds1_id)
        first = execution.claim()
        self.assertEqual(first[1], 1)
        # 模拟 worker 中断：租约到期后被重新认领，attempt 递增但仍是同一条运行。
        with Session.begin() as db:
            job = db.get(Run, r1, with_for_update=True)
            job.lease_until = now() - timedelta(minutes=1)
        second = execution.claim()
        self.assertEqual(second[0], r1)
        self.assertEqual(second[1], 2)
        execution.execute(second)
        # 迟到的第一次执行结果必须被丢弃，不能覆盖恢复后的结果。
        execution.execute(first)
        with Session() as db:
            job = db.get(Run, r1)
            self.assertEqual(job.status, Status.COMPLETE)
            self.assertEqual(job.attempt, 2)
            self.assertEqual(db.scalar(select(ReportPublication).where(
                ReportPublication.run_id == r1, ReportPublication.state == PublicationState.ACTIVE)).version, 1)

    def test_04_older_finished_run_cannot_overwrite_newer(self):
        r1 = self._enqueue(self.ds1_id)
        self._run_to_completion(r1)
        r2 = self._enqueue(self.ds2_id)
        self._run_to_completion(r2)
        with Session.begin() as db:
            old_job = db.get(Run, r1, with_for_update=True)
            publication, error = publish_run(db, old_job, old_job.result, old_job.export_file)
            self.assertIsNone(publication)
            self.assertIn('更新版本', error)
            active = db.scalar(select(ReportPublication).where(
                ReportPublication.report_day == DAY, ReportPublication.state == PublicationState.ACTIVE))
            self.assertEqual(active.run_id, r2)

    def test_05_consistency_gate_blocks_switch_and_keeps_old_version(self):
        r1 = self._enqueue(self.ds1_id)
        self._run_to_completion(r1)
        r2 = self._enqueue(self.ds2_id)
        lease = execution.claim()
        result = analyze(lease[2], lease[3])
        export_name = write_report(lease[0], lease[1], result)
        # 底稿在发布前丢失：门控必须拒绝切换，旧版继续生效。
        (ARTIFACTS / export_name).unlink()
        with Session.begin() as db:
            job = db.get(Run, r2, with_for_update=True)
            publication, error = publish_run(db, job, result, export_name)
            self.assertIsNone(publication)
            self.assertIsNotNone(error)
            rejected = db.scalar(select(ReportPublication).where(
                ReportPublication.run_id == r2, ReportPublication.state == PublicationState.REJECTED))
            self.assertIsNotNone(rejected)
            active = db.scalar(select(ReportPublication).where(
                ReportPublication.report_day == DAY, ReportPublication.state == PublicationState.ACTIVE))
            self.assertEqual(active.run_id, r1)
            self.assertIsNone(job.publication_id)

    def test_06_signed_report_and_delivery_survive_new_version(self):
        schedule_id = self._schedule()
        r1 = self._enqueue(self.ds1_id, schedule_id=schedule_id, scheduled_at=now())
        self._run_to_completion(r1)
        with Session.begin() as db:
            job = db.get(Run, r1, with_for_update=True)
            job.review = Review.ACCEPTED
            job.review_note = '主管已签收'
            job.decision = '维持排期'
        r2 = self._enqueue(self.ds2_id, schedule_id=schedule_id, scheduled_at=now() + timedelta(hours=1))
        self._run_to_completion(r2)
        with Session() as db:
            old = db.get(Run, r1)
            self.assertEqual(old.review, Review.ACCEPTED)
            self.assertEqual(old.review_note, '主管已签收')
            delivery = db.scalars(select(Delivery).where(Delivery.run_id == r1)).all()
            self.assertEqual(len(delivery), 1)
            # 新版只产生自己的回执，不重复生成旧运行的签收。
            self.assertEqual(len(db.scalars(select(Delivery)).all()), 2)
            self.assertTrue((ARTIFACTS / old.export_file).is_file())

    def test_07_csv_artifact_carries_same_fingerprint_as_page(self):
        r1 = self._enqueue(self.ds1_id)
        job = self._run_to_completion(r1)
        path = ARTIFACTS / job.export_file
        with path.open(encoding='utf-8-sig', newline='') as handle:
            content = list(csv.reader(handle))
        flat = [cell for row in content for cell in row]
        self.assertIn(job.result['fingerprint'], flat)
        self.assertIn(job.result['snapshot']['ledger_fingerprint'], flat)
        # 桥接表与复算底稿的标题都在同一份 CSV 内。
        self.assertTrue(any('全剧场计划到实收桥接' in row for row in content))
        self.assertTrue(any(f'复算底稿 · {DAY}' in row for row in content))

    def test_08_scheduled_materialization_is_idempotent(self):
        schedule_id = self._schedule(next_at=now() - timedelta(hours=1))
        execution.materialize_due()
        execution.materialize_due()
        with Session() as db:
            runs = db.scalars(select(Run).where(Run.schedule_id == schedule_id)).all()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].params['report_day'], DAY)
            plan = db.get(Schedule, schedule_id)
            self.assertGreater(plan.next_at, now())
            self.assertEqual(plan.params['report_day'], '2026-11-24')

    def test_09_identical_content_batch_dedupes_at_enqueue(self):
        r1 = self._enqueue(self.ds1_id)
        self._run_to_completion(r1)
        # 内容完全相同的另一批次：台账指纹相同，幂等键随之相同，补跑请求被去重而不是生成第二个版本。
        with Session.begin() as db:
            mirror, created = save(db, '同内容补跑批次', 'mirror.csv', self.base_rows, 'analyst')
            self.assertTrue(created)
            dataset = db.get(Dataset, mirror.id)
            key = idem_key(DAY, dataset.checksum, self.params)
            db.add(Run(dataset_id=mirror.id, name='补跑', params=dict(self.params),
                       author='analyst', idem_key=key))
            with self.assertRaises(IntegrityError):
                db.flush()
        with Session() as db:
            pubs = db.scalars(select(ReportPublication).where(ReportPublication.report_day == DAY)).all()
            self.assertEqual(len(pubs), 1)
            self.assertEqual(db.scalar(select(Run).where(Run.id == r1)).publication_id, pubs[0].id)

    def _checksum(self, dataset_id):
        with Session() as db:
            return db.get(Dataset, dataset_id).checksum

    def _schedule(self, next_at=None):
        with Session.begin() as db:
            item = Schedule(name='每日重算', dataset_id=self.ds1_id, params=dict(self.params),
                            recipient='剧场财务结算组', next_at=next_at or (now() + timedelta(days=1)),
                            repeat_daily=True, enabled=True, author='admin')
            db.add(item)
            db.flush()
            return item.id


if __name__ == '__main__':
    unittest.main()
