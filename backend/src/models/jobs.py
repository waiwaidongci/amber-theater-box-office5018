from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import PublicationState, Review, Status
from src.utils.time import now

class Schedule(Base):
    __tablename__ = 'schedules'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    params: Mapped[dict] = mapped_column(JSON)
    recipient: Mapped[str] = mapped_column(String(100))
    next_at: Mapped[datetime] = mapped_column(index=True)
    repeat_daily: Mapped[bool]
    enabled: Mapped[bool] = mapped_column(default=True)
    author: Mapped[str] = mapped_column(String(64))

class Run(Base):
    __tablename__ = 'runs'
    __table_args__ = (
        UniqueConstraint('schedule_id', 'scheduled_at'),
        # 同一营业日、同一台账快照与同一参数口径只允许一个运行，重跑复用而不是重复生成。
        UniqueConstraint('idem_key'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime | None]
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default=Status.QUEUED, index=True)
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    export_file: Mapped[str | None] = mapped_column(String(100))
    review: Mapped[str] = mapped_column(String(20), default=Review.PENDING)
    review_note: Mapped[str | None] = mapped_column(String(500))
    decision: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    idem_key: Mapped[str | None] = mapped_column(String(120), index=True)
    # 与 ReportPublication 形成循环外键，use_alter 保证建表/删表顺序可解析。
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey('report_publications.id', use_alter=True))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]

class ReportPublication(Base):
    """按演出营业日维护的已发布版本。计算期间读取方只看到旧版，
    桥接、渠道处置与 CSV 底稿一致并在同一事务内切换。"""
    __tablename__ = 'report_publications'
    __table_args__ = (
        UniqueConstraint('report_day', 'version'),
        UniqueConstraint('run_id'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_day: Mapped[str] = mapped_column(String(10), index=True)
    version: Mapped[int]
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    dataset_checksum: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    export_file: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default=PublicationState.ACTIVE, index=True)
    published_at: Mapped[datetime] = mapped_column(default=now)
    superseded_at: Mapped[datetime | None]

class Delivery(Base):
    __tablename__ = 'deliveries'

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), unique=True)
    recipient: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)
