from datetime import datetime
from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now

class Dataset(Base):
    __tablename__ = 'datasets'
    __table_args__ = (UniqueConstraint('name', 'checksum'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    row_count: Mapped[int]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)

class DatasetRevision(Base):
    """同一业务批次的一版特征快照，第 1 版为导入时的原始台账。"""
    __tablename__ = 'dataset_revisions'
    __table_args__ = (UniqueConstraint('dataset_id', 'revision'),
                      UniqueConstraint('dataset_id', 'checksum'))

    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    row_count: Mapped[int]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)

class PairDistance(Base):
    """冻结到版本的规格对距离，支撑中断恢复与下一版按改动对增量重算。"""
    __tablename__ = 'pair_distances'
    __table_args__ = (UniqueConstraint('dataset_id', 'revision', 'left_index', 'right_index'),
                      Index('ix_pair_distances_dataset_revision', 'dataset_id', 'revision'))

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    revision: Mapped[int] = mapped_column()
    left_index: Mapped[int] = mapped_column()
    right_index: Mapped[int] = mapped_column()
    distance: Mapped[float] = mapped_column()

class PublishedRevision(Base):
    """每个批次的已发布版本指针，每批至多一条，只接受更新的版本。"""
    __tablename__ = 'published_revisions'

    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), primary_key=True)
    revision: Mapped[int] = mapped_column()
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    published_at: Mapped[datetime] = mapped_column(default=now)
