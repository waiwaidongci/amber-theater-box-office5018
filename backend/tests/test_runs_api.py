"""HTTP 层测试：幂等建运行、发布版本读取与下载、旧任务不覆盖新版。

cd backend && python3 -m unittest tests.test_runs_api -v
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix='hxwl-api-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP}/api.db')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', f'{_TMP}/artifacts')
os.environ.setdefault('SEED_DEMO', 'false')

from fastapi.testclient import TestClient

from src.config.database import Base, Session, engine
from src.config.migrations import run_migrations
from src.constants.enums import PublicationState, Role, Status
from src.domain.demo import demo_rows
from src.main import app
from src.models.identity import User
from src.models.jobs import ReportPublication, Run
from src.models.source import Dataset
from src.services import execution
from src.services.datasets import save
from src.utils.security import hash_password
from src.utils.wakeup import broker


class FakeRedis:
    def __init__(self, *args, **kwargs):
        pass

    def ping(self):
        return True

    def pipeline(self):
        class Pipe:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def lpush(self, *a):
                return self

            def ltrim(self, *a):
                return self

            def execute(self):
                return []
        return Pipe()

    def eval(self, *a):
        return 1


import src.utils.wakeup as wakeup_module
wakeup_module.broker = FakeRedis()
import src.middlewares.rate as rate_module
rate_module.broker = FakeRedis()

DAY = '2026-11-23'


class RunsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        run_migrations()
        with Session.begin() as db:
            db.add(User(username=Role.ANALYST, role=Role.ANALYST,
                        password_hash=hash_password('analyst-demo-2611')))
            db.add(User(username=Role.VIEWER, role=Role.VIEWER,
                        password_hash=hash_password('viewer-demo-2611')))
        cls.client = TestClient(app)
        reply = cls.client.post('/auth/login', json={'username': 'analyst', 'password': 'analyst-demo-2611'})
        cls.token = reply.json()['token']
        cls.headers = {'Authorization': f'Bearer {cls.token}'}
        with Session.begin() as db:
            dataset, _ = save(db, 'API 快照', 'api.csv', demo_rows(), 'analyst')
            cls.dataset_id = dataset.id

    def setUp(self):
        with Session.begin() as db:
            db.query(ReportPublication).delete()
            db.query(Run).delete()

    def _start(self):
        return self.client.post('/runs', headers=self.headers,
                                json={'name': '接口运行', 'dataset_id': self.dataset_id,
                                      'params': {'report_day': DAY}})

    def test_01_create_is_idempotent_and_publishes(self):
        first = self._start()
        self.assertEqual(first.status_code, 202)
        run_id = first.json()['id']
        second = self._start()
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()['id'], run_id, '并发/重复提交必须复用同一条运行')
        lease = execution.claim()
        execution.execute(lease)
        detail = self.client.get(f'/runs/{run_id}', headers=self.headers).json()
        self.assertEqual(detail['status'], Status.COMPLETE)
        self.assertEqual(detail['publication']['state'], PublicationState.ACTIVE)
        self.assertEqual(detail['publication']['version'], 1)

    def test_02_published_endpoint_serves_active_version_only(self):
        run_id = self._start().json()['id']
        missing = self.client.get(f'/runs/published/{DAY}', headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        execution.execute(execution.claim())
        published = self.client.get(f'/runs/published/{DAY}', headers=self.headers)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()['id'], run_id)
        self.assertEqual(published.json()['publication']['report_day'], DAY)

    def test_03_export_downloads_published_artifact(self):
        run_id = self._start().json()['id']
        execution.execute(execution.claim())
        reply = self.client.get(f'/runs/{run_id}/export', headers=self.headers)
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.text.startswith('﻿'))
        self.assertIn('结果指纹', reply.text)
        self.assertIn('全剧场计划到实收桥接', reply.text)

    def test_04_viewer_cannot_start_runs(self):
        reply = self.client.post('/auth/login', json={'username': 'viewer', 'password': 'viewer-demo-2611'})
        headers = {'Authorization': f"Bearer {reply.json()['token']}"}
        denied = self.client.post('/runs', headers=headers,
                                  json={'name': 'x', 'dataset_id': self.dataset_id, 'params': {}})
        self.assertEqual(denied.status_code, 403)

    def test_05_concurrent_inserts_fall_back_to_existing_run(self):
        from src.routes import runs as runs_route
        from src.models.identity import User as UserModel
        from src.services.publication import idem_key
        from src.services.validation import normalize_params

        actor = UserModel(username=Role.ANALYST, role=Role.ANALYST)
        params = normalize_params({'report_day': DAY})
        with Session.begin() as db:
            checksum = db.get(Dataset, self.dataset_id).checksum
            existing = Run(name='先到请求', dataset_id=self.dataset_id, params=dict(params),
                           author='analyst', idem_key=idem_key(DAY, checksum, params))
            db.add(existing)
            db.flush()
            existing_id = existing.id

        body = type('Body', (), {'name': '后到请求', 'dataset_id': self.dataset_id,
                                 'params': {'report_day': DAY}})()
        # 模拟并发：预检查 SELECT 未命中（另一事务尚未提交），INSERT 由唯一约束兜底；
        # 回滚后的兜底查询恢复真实行为，返回已存在的运行。
        from sqlalchemy.orm import Session as OrmSession
        original_scalar = OrmSession.scalar
        calls = {'n': 0}

        def patched_scalar(self, statement):
            calls['n'] += 1
            return None if calls['n'] == 1 else original_scalar(self, statement)

        OrmSession.scalar = patched_scalar
        try:
            with Session() as session_b:
                view = runs_route.create(body, db=session_b, actor=actor)
        finally:
            OrmSession.scalar = original_scalar
        self.assertEqual(view['id'], existing_id)
        self.assertEqual(view['name'], '先到请求')
        with Session() as db:
            self.assertEqual(db.query(Run).filter(
                Run.idem_key == idem_key(DAY, checksum, params)).count(), 1)


if __name__ == '__main__':
    unittest.main()
