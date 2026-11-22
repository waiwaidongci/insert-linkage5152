import csv
import io
import os
import tempfile
from pathlib import Path

os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:////tmp/unused.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')


def csv_text(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=('spec_id', 'name', 'fit_traits'))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.config import database
    from src.config.database import Base
    import src.models.identity  # noqa: F401
    import src.models.jobs  # noqa: F401
    import src.models.source  # noqa: F401
    from src.models.identity import User
    from src.utils.security import hash_password

    tmp = tempfile.mkdtemp()
    from src.config import settings
    from src.utils import exports
    from src.routes import distribution
    settings.ARTIFACTS = Path(tmp)
    exports.ARTIFACTS = Path(tmp)
    distribution.ARTIFACTS = Path(tmp)

    engine = create_engine('sqlite+pysqlite:///:memory:',
                           connect_args={'check_same_thread': False}, poolclass=StaticPool)
    testing = sessionmaker(engine, expire_on_commit=False)
    database.engine = engine
    database.Session = testing
    from src.services import execution
    execution.Session = testing
    from src import main as main_module
    main_module.engine = engine
    Base.metadata.create_all(engine)
    with testing.begin() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(username=role, role=role, password_hash=hash_password(f'{role}-pw')))

    from src.domain.demo import demo_rows
    from src.services.datasets import save
    with testing.begin() as db:
        dataset, created = save(db, '演示批次', 'demo.csv', demo_rows(), 'analyst')
        dataset_id = dataset.id
        assert created

    client = TestClient(main_module.app)

    def login(role):
        token = client.post('/auth/login', json={'username': role, 'password': f'{role}-pw'}).json()['token']
        return {'Authorization': f'Bearer {token}'}

    admin, analyst, viewer = login('admin'), login('analyst'), login('viewer')

    # 导入即建立第 1 版快照
    lineage = client.get(f'/datasets/{dataset_id}/revisions', headers=viewer).json()
    assert [item['revision'] for item in lineage] == [1], lineage

    # viewer 只读，不能登记更正
    forbidden = client.post(f'/datasets/{dataset_id}/revisions',
                            json={'filename': 'x.csv', 'csv_text': csv_text(demo_rows())}, headers=viewer)
    assert forbidden.status_code == 403

    # 登记第 2 版更正（改 CP-B03）
    rows_v2 = [dict(r) for r in demo_rows()]
    for row in rows_v2:
        if row['spec_id'] == 'CP-B03':
            row['fit_traits'] = '400×300 箱座|弧面底托|纵向加强筋|传感器让位|防静电接触层'
    resp = client.post(f'/datasets/{dataset_id}/revisions',
                       json={'filename': 'fix.csv', 'csv_text': csv_text(rows_v2)}, headers=analyst)
    assert resp.status_code == 201, resp.text
    assert resp.json()['revision'] == 2

    # 同内容重复提交幂等
    again = client.post(f'/datasets/{dataset_id}/revisions',
                        json={'filename': 'fix.csv', 'csv_text': csv_text(rows_v2)}, headers=analyst)
    assert again.json()['created'] is False and again.json()['revision'] == 2

    # 未发布前下载最新版应 409
    no_publish = client.get(f'/datasets/{dataset_id}/latest-export', headers=viewer)
    assert no_publish.status_code == 409

    # 启动第 2 版运行并由 worker 逻辑完成
    run_resp = client.post('/runs', json={'dataset_id': dataset_id, 'name': '第2版',
                                           'params': {'cut_distance': 0.6}}, headers=analyst)
    assert run_resp.status_code == 202
    run_id = run_resp.json()['id']
    assert run_resp.json()['revision'] == 2
    execution.execute(execution.claim())
    detail = client.get(f'/runs/{run_id}', headers=viewer).json()
    assert detail['status'] == 'complete' and detail['published'] is True

    # 版本链反映发布状态
    lineage = client.get(f'/datasets/{dataset_id}/revisions', headers=viewer).json()
    assert lineage[0]['published'] is False and lineage[1]['published'] is True

    # 采购下载最新完整版本
    latest = client.get(f'/datasets/{dataset_id}/latest-export', headers=viewer)
    assert latest.status_code == 200
    assert 'r2.csv' in latest.headers['content-disposition']

    # 手工构造一个 revision=1 的运行，验证旧版晚完成时的发布保护
    from src.models.jobs import Run
    with testing.begin() as db:
        stale = Run(dataset_id=dataset_id, name='旧版', revision=1,
                    params={'cut_distance': 0.6}, author='analyst')
        db.add(stale)
        db.flush()
        stale_id = stale.id
    execution.execute(execution.claim())
    stale_view = client.get(f'/runs/{stale_id}', headers=viewer).json()
    assert stale_view['superseded'] is True and stale_view['published'] is False
    assert '已发布' in stale_view['result']['note']
    # 旧版不能再签核
    review = client.post(f'/runs/{stale_id}/review',
                         json={'review': 'accepted', 'decision': '安排共用打样', 'note': '试签旧版'},
                         headers=admin)
    assert review.status_code == 409
    # 发布指针仍是第 2 版，下载内容不变
    assert client.get(f'/datasets/{dataset_id}/latest-export', headers=viewer).status_code == 200

    # 原签核文件仍可从各运行下载
    assert client.get(f'/runs/{run_id}/export', headers=viewer).status_code == 200

    # 审计记录包含更正与发布相关事件
    audits = client.get('/audit', headers=admin).json()
    events = {item['event'] for item in audits}
    assert 'revision.registered' in events
    assert 'analysis.superseded' in events
    assert 'delivery.latest_download' in events

    print('HTTP END-TO-END OK')


if __name__ == '__main__':
    main()
