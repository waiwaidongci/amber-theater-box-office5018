from enum import StrEnum

class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'

class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'

class PublicationState(StrEnum):
    ACTIVE = 'active'          # 当前对外生效的版本，页面与下载均指向它
    SUPERSEDED = 'superseded'  # 已被新版一次性切换替代，原 CSV、签收与复核记录保留
    REJECTED = 'rejected'      # 一致性校验未通过，未对外发布

class Review(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    RETURNED = 'returned'
