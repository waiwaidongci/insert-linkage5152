import csv
import io

import pytest

from src.constants.enums import Status
from src.domain import analysis, revisions
from src.domain.demo import demo_rows
from src.models.jobs import Run
from src.models.source import DatasetRevision, PairDistance, PublishedRevision
from src.services.datasets import save
from src.services.execution import claim, execute
from src.utils.security import token

HEADERS = ('spec_id,name,fit_traits\n')


def csv_text(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=('spec_id', 'name', 'fit_traits'))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def changed_rows(rows, spec_id, new_traits):
    edited = [dict(row) for row in rows]
    for row in edited:
        if row['spec_id'] == spec_id:
            row['fit_traits'] = new_traits
    return edited


def auth(client, username='admin'):
    response = client.post('/auth/login', json={'username': username, 'password': f'{username}-pw'})
    assert response.status_code == 200
    return {'Authorization': f"Bearer {response.json()['token']}"}


def seed_dataset(db_session, name='演示批次'):
    with db_session.begin() as db:
        dataset, _ = save(db, name, 'demo.csv', demo_rows(), 'analyst')
        dataset_id = dataset.id
    return dataset_id


def start_run(db_session, dataset_id, revision):
    with db_session.begin() as db:
        run = Run(dataset_id=dataset_id, name=f'第 {revision} 版重算', revision=revision,
                  params={'cut_distance': 0.6}, author='analyst')
        db.add(run)
        db.flush()
        return run.id


def test_incremental_recompute_matches_full_recompute(db_session):
    dataset_id = seed_dataset(db_session)
    baseline = analysis.analyze(demo_rows(), {'cut_distance': 0.6})

    with db_session.begin() as db:
        run_v1 = Run(dataset_id=dataset_id, name='第 1 版', revision=1,
                     params={'cut_distance': 0.6}, author='analyst')
        db.add(run_v1)
        db.flush()
        run_v1_id = run_v1.id

    execute(claim())
    with db_session.begin() as db:
        completed = db.get(Run, run_v1_id)
        assert completed.status == Status.COMPLETE
        published = db.get(PublishedRevision, dataset_id)
        assert published.revision == 1
        pair_rows = db.query(PairDistance).filter_by(dataset_id=dataset_id, revision=1).count()
        assert pair_rows == 28  # 8 个规格的全部下三角规格对

    rows_v2 = changed_rows(demo_rows(), 'CP-B03',
                           '400×300 箱座|弧面底托|纵向加强筋|传感器让位|防静电接触层')
    full = analysis.analyze(rows_v2, {'cut_distance': 0.6})
    with db_session.begin() as db:
        from src.models.source import Dataset
        revision, created = revisions.register_revision(db, db.get(Dataset, dataset_id), rows_v2, 'analyst')
        assert created and revision.revision == 2
        run_v2 = Run(dataset_id=dataset_id, name='第 2 版', revision=2,
                     params={'cut_distance': 0.6}, author='analyst')
        db.add(run_v2)
        db.flush()
        run_v2_id = run_v2.id

    execute(claim())
    with db_session.begin() as db:
        completed = db.get(Run, run_v2_id)
        assert completed.status == Status.COMPLETE
        # 切树与整批重算完全一致
        assert completed.result['tables'][0]['rows'] == full['tables'][0]['rows']
        assert completed.result['tables'][1]['rows'] == full['tables'][1]['rows']
        # 只重算至少一端涉及 CP-B03 的 7 个规格对，其余 21 个沿用上一版
        metric = next(item for item in completed.result['metrics'] if item['label'] == '本版重算规格对')
        assert metric['value'] == 7
        assert '21 个规格对沿用前版' in metric['detail']
        pair_rows = db.query(PairDistance).filter_by(dataset_id=dataset_id, revision=2).count()
        assert pair_rows == 28
        assert db.get(PublishedRevision, dataset_id).revision == 2


def test_concurrent_versions_stale_one_cannot_overtake_newer(db_session):
    dataset_id = seed_dataset(db_session)
    rows = demo_rows()
    rows_v2 = changed_rows(rows, 'CP-B01', '400×300 箱座|弧面底托|左侧定位孔|纵向加强筋')
    rows_v3 = changed_rows(rows_v2, 'CP-C01', '300×200 箱座|八格分隔|标签卡位')

    with db_session.begin() as db:
        from src.models.source import Dataset
        dataset = db.get(Dataset, dataset_id)
        rev2, _ = revisions.register_revision(db, dataset, rows_v2, 'analyst')
        rev3, _ = revisions.register_revision(db, dataset, rows_v3, 'analyst')
        assert rev2.revision == 2 and rev3.revision == 3
        # 前版未算完时仍可登记下一次更正，三个版本的运行并发排队
        for revision in (1, 2, 3):
            db.add(Run(dataset_id=dataset_id, name=f'第 {revision} 版', revision=revision,
                       params={'cut_distance': 0.6}, author='analyst'))

    run_ids = {}
    leases = {}
    # 三个版本并发排队：先领走全部租约，再按乱序完成，模拟旧版晚完成。
    for _ in range(3):
        lease = claim()
        assert lease is not None
        job_id, attempt, revision, _rows, _params = lease
        run_ids[revision] = job_id
        leases[revision] = lease

    for revision in (3, 2):
        execute(leases[revision])

    with db_session.begin() as db:
        assert db.get(PublishedRevision, dataset_id).revision == 3
        assert db.get(Run, run_ids[2]).status == Status.COMPLETE
        assert db.get(Run, run_ids[3]).status == Status.COMPLETE
        assert db.get(Run, run_ids[2]).superseded is True

    execute(leases[1])  # 旧版第 1 版最后完成
    with db_session.begin() as db:
        stale = db.get(Run, run_ids[1])
        assert stale.status == Status.COMPLETE
        assert stale.superseded is True
        # 发布指针仍是第 3 版，晚完成的旧版没有顶掉新版
        pointer = db.get(PublishedRevision, dataset_id)
        assert pointer.revision == 3 and pointer.run_id == run_ids[3]
        assert '已发布' in stale.result['note']


def test_interrupted_resume_skips_finished_pairs(db_session):
    dataset_id = seed_dataset(db_session)
    rows_v2 = changed_rows(demo_rows(), 'CP-A01',
                           '600×400 箱座|四角缓冲|双销定位|防静电接触层')
    run_v2_id = start_run(db_session, dataset_id, 1)
    execute(claim())

    with db_session.begin() as db:
        from src.models.source import Dataset
        revision, _ = revisions.register_revision(
            db, db.get(Dataset, dataset_id), rows_v2, 'analyst')
        assert revision.revision == 2
        run = Run(dataset_id=dataset_id, name='中断的第 2 版', revision=2,
                  params={'cut_distance': 0.6}, author='analyst')
        db.add(run)
        db.flush()
        run_id = run.id
        # 模拟中断前已算完 4 个涉及 CP-A01（下标 0）的规格对，按本版特征写入正确距离
        sorted_rows = sorted(analysis.validate_rows(rows_v2),
                             key=lambda item: item['spec_id'].casefold())
        traits = [set(row['fit_traits'].split('|')) for row in sorted_rows]
        for right in range(1, 5):
            db.add(PairDistance(dataset_id=dataset_id, revision=2, left_index=0,
                                right_index=right, distance=analysis.pair_distance(traits, 0, right)))

    execute(claim())
    with db_session.begin() as db:
        run = db.get(Run, run_id)
        assert run.status == Status.COMPLETE
        full = analysis.analyze(rows_v2, {'cut_distance': 0.6})
        assert run.result['tables'][0]['rows'] == full['tables'][0]['rows']
        pairs = {(item.left_index, item.right_index): item.distance
                 for item in db.query(PairDistance).filter_by(dataset_id=dataset_id, revision=2)}
        assert len(pairs) == 28
        # 恢复时沿用该版特征，已算完的 4 对未重算，剩余 3 个受影响规格对补算完成
        for right in range(1, 8):
            assert pairs[(0, right)] == analysis.pair_distance(traits, 0, right)
        metric = next(item for item in run.result['metrics'] if item['label'] == '本版重算规格对')
        # 受影响共 7 对，其中 4 对已在中断前算完，恢复后只补算 3 对
        assert metric['value'] == 3
        assert '21 个规格对沿用前版' in metric['detail']


def test_checkpoint_committed_pairs_survive_crash(db_session):
    """检查点事务提交后即使切树前进程崩溃，已算完的规格对在新会话中仍然保留并被跳过。"""
    dataset_id = seed_dataset(db_session)
    start_run(db_session, dataset_id, 1)
    execute(claim())
    rows_v2 = changed_rows(demo_rows(), 'CP-B02', '400×300 箱座|弧面底托|纵向加强筋')
    with db_session.begin() as db:
        from src.models.source import Dataset
        revisions.register_revision(db, db.get(Dataset, dataset_id), rows_v2, 'analyst')
        run = Run(dataset_id=dataset_id, name='崩溃的第 2 版', revision=2,
                  params={'cut_distance': 0.6}, author='analyst')
        db.add(run)
        db.flush()
        run_id = run.id

    # 第一步：只执行检查点事务并独立提交，随后模拟进程崩溃（不执行切树）。
    with db_session.begin() as db:
        job = db.get(Run, run_id, with_for_update=True)
        ids, traits, progress = revisions.ensure_pair_distances(db, job)
        job.pair_progress = progress
    committed = {}
    with db_session() as db:
        for item in db.query(PairDistance).filter_by(dataset_id=dataset_id, revision=2):
            committed[(item.left_index, item.right_index)] = item.distance
    assert len(committed) == 28  # 检查点已把全部规格对距离独立落库

    # 第二步：模拟崩溃后租约过期，worker 重新领取并恢复（沿用检查点，不重算规格对）。
    from src.utils.time import now
    from datetime import timedelta
    with db_session.begin() as db:
        job = db.get(Run, run_id)
        job.status = Status.RUNNING
        job.attempt = 1
        job.lease_until = now() - timedelta(minutes=1)
    execute(claim())
    with db_session.begin() as db:
        run = db.get(Run, run_id)
        assert run.status == Status.COMPLETE
        full = analysis.analyze(rows_v2, {'cut_distance': 0.6})
        assert run.result['tables'][0]['rows'] == full['tables'][0]['rows']
        recovered = {}
        for item in db.query(PairDistance).filter_by(dataset_id=dataset_id, revision=2):
            recovered[(item.left_index, item.right_index)] = item.distance
        assert recovered == committed  # 恢复沿用检查点，未重算任何规格对
        metric = next(item for item in run.result['metrics'] if item['label'] == '本版重算规格对')
        # CP-B02 涉及 7 个受影响规格对，全部在崩溃前的检查点算完，恢复后补算为 0
        assert metric['value'] == 7


def test_newer_revision_runs_before_previous_finishes(db_session):
    """v2 尚未计算（无任何缓存）时 v3 先跑：缺失缓存的规格对直接重算，切树仍等于整批重算。"""
    dataset_id = seed_dataset(db_session)
    start_run(db_session, dataset_id, 1)
    execute(claim())
    rows = demo_rows()
    rows_v2 = changed_rows(rows, 'CP-A02', '600×400 箱座|四角缓冲|双销定位|防静电接触层')
    rows_v3 = changed_rows(rows_v2, 'CP-C01', '300×200 箱座|八格分隔|标签卡位')
    with db_session.begin() as db:
        from src.models.source import Dataset
        dataset = db.get(Dataset, dataset_id)
        revisions.register_revision(db, dataset, rows_v2, 'analyst')
        revisions.register_revision(db, dataset, rows_v3, 'analyst')
        # 只登记 v3 的运行；v2 已登记更正但尚未发起计算，模拟两版并发中的乱序完成。
        run = Run(dataset_id=dataset_id, name='抢先的第 3 版', revision=3,
                  params={'cut_distance': 0.6}, author='analyst')
        db.add(run)
        db.flush()
        run_id = run.id

    execute(claim())
    full = analysis.analyze(rows_v3, {'cut_distance': 0.6})
    with db_session.begin() as db:
        completed = db.get(Run, run_id)
        assert completed.status == Status.COMPLETE
        assert completed.result['tables'][0]['rows'] == full['tables'][0]['rows']
        assert completed.result['tables'][1]['rows'] == full['tables'][1]['rows']
        assert db.get(PublishedRevision, dataset_id).revision == 3
        # v2 缓存完全缺失，28 对全部由 v3 直接重算，结果仍与整批重算一致
        metric = next(item for item in completed.result['metrics'] if item['label'] == '本版重算规格对')
        assert metric['value'] == 28 and metric['detail'].startswith('至少一端较前版有改动；0')


def test_added_and_removed_specs_recompute_correct_pairs(db_session):
    """新增规格只重算涉及它的规格对；删除规格后剩余规格的距离按编号正确沿用。"""
    dataset_id = seed_dataset(db_session)
    start_run(db_session, dataset_id, 1)
    execute(claim())

    # 新增一个规格
    rows_with_new = demo_rows() + [
        {'spec_id': 'CP-D01', 'name': '新增箱体内衬', 'fit_traits': '600×400 箱座|四角缓冲'}]
    full_added = analysis.analyze(rows_with_new, {'cut_distance': 0.6})
    with db_session.begin() as db:
        from src.models.source import Dataset
        revision, _ = revisions.register_revision(
            db, db.get(Dataset, dataset_id), rows_with_new, 'analyst')
        assert revision.revision == 2
        run = Run(dataset_id=dataset_id, name='新增规格第 2 版', revision=2,
                  params={'cut_distance': 0.6}, author='analyst')
        db.add(run)
        db.flush()
        run_id = run.id
    execute(claim())
    with db_session.begin() as db:
        completed = db.get(Run, run_id)
        assert completed.status == Status.COMPLETE
        assert completed.result['tables'][0]['rows'] == full_added['tables'][0]['rows']
        assert completed.result['tables'][1]['rows'] == full_added['tables'][1]['rows']
        # 9 个规格共 36 对，新规格涉及 8 对需重算，其余 28 对沿用
        metric = next(item for item in completed.result['metrics'] if item['label'] == '本版重算规格对')
        assert metric['value'] == 8 and '28 个规格对沿用前版' in metric['detail']

    # 在第 2 版基础上删除一个规格
    rows_removed = [row for row in rows_with_new if row['spec_id'] != 'CP-A03']
    full_removed = analysis.analyze(rows_removed, {'cut_distance': 0.6})
    with db_session.begin() as db:
        from src.models.source import Dataset
        revision, _ = revisions.register_revision(
            db, db.get(Dataset, dataset_id), rows_removed, 'analyst')
        assert revision.revision == 3
        run = Run(dataset_id=dataset_id, name='删除规格第 3 版', revision=3,
                  params={'cut_distance': 0.6}, author='analyst')
        db.add(run)
        db.flush()
        run_id = run.id
    execute(claim())
    with db_session.begin() as db:
        completed = db.get(Run, run_id)
        assert completed.status == Status.COMPLETE
        assert completed.result['tables'][0]['rows'] == full_removed['tables'][0]['rows']
        assert completed.result['tables'][1]['rows'] == full_removed['tables'][1]['rows']
        # 8 个剩余规格共 28 对，涉及被删 CP-A03 的 8 对消失，剩余 28 对特征未变、全部沿用
        metric = next(item for item in completed.result['metrics'] if item['label'] == '本版重算规格对')
        assert metric['value'] == 0 and '28 个规格对沿用前版' in metric['detail']


def test_duplicate_revision_registration_is_idempotent(db_session):
    dataset_id = seed_dataset(db_session)
    rows_v2 = changed_rows(demo_rows(), 'CP-B02', '400×300 箱座|弧面底托|纵向加强筋')
    with db_session.begin() as db:
        from src.models.source import Dataset
        dataset = db.get(Dataset, dataset_id)
        first, created = revisions.register_revision(db, dataset, rows_v2, 'analyst')
        assert created and first.revision == 2
        second, created_again = revisions.register_revision(db, dataset, rows_v2, 'analyst')
        assert not created_again and second.revision == 2
        assert db.query(DatasetRevision).filter_by(dataset_id=dataset_id).count() == 2


def test_end_to_end_api_flow_and_latest_export(client, db_session):
    headers = auth(client)
    dataset_id = seed_dataset(db_session)

    # 登记更正
    rows_v2 = changed_rows(demo_rows(), 'CP-B03',
                           '400×300 箱座|弧面底托|纵向加强筋|传感器让位|防静电接触层')
    response = client.post(f'/datasets/{dataset_id}/revisions',
                           json={'filename': 'correction.csv', 'csv_text': csv_text(rows_v2)},
                           headers=headers)
    assert response.status_code == 201
    assert response.json()['revision'] == 2 and response.json()['created'] is True

    lineage = client.get(f'/datasets/{dataset_id}/revisions', headers=headers).json()
    assert [item['revision'] for item in lineage] == [1, 2]
    assert lineage[1]['published'] is False

    # 启动版本化运行并等待 worker 逻辑执行
    run_id = start_run(db_session, dataset_id, 2)
    execute(claim())

    detail = client.get(f'/runs/{run_id}', headers=headers).json()
    assert detail['status'] == 'complete' and detail['revision'] == 2
    lineage = client.get(f'/datasets/{dataset_id}/revisions', headers=headers).json()
    assert lineage[1]['published'] is True

    # 采购在交付页面下载最新完整版本
    response = client.get(f'/datasets/{dataset_id}/latest-export', headers=headers)
    assert response.status_code == 200
    disposition = response.headers['content-disposition']
    assert f'r2.csv' in disposition and 'attachment' in disposition
    assert '切出' in response.content.decode('utf-8-sig')

    # 原签核文件仍可从运行记录下载
    signed = client.get(f'/runs/{run_id}/export', headers=headers)
    assert signed.status_code == 200


def test_superseded_run_cannot_be_signed(client, db_session):
    dataset_id = seed_dataset(db_session)
    rows_v2 = changed_rows(demo_rows(), 'CP-C02', '300×200 箱座|八格分隔|标签卡位|防静电接触层')
    with db_session.begin() as db:
        from src.models.source import Dataset
        revisions.register_revision(db, db.get(Dataset, dataset_id), rows_v2, 'analyst')
        for revision in (1, 2):
            db.add(Run(dataset_id=dataset_id, name=f'第 {revision} 版', revision=revision,
                       params={'cut_distance': 0.6}, author='analyst'))
    first = claim()
    execute(first)
    second = claim()
    execute(second)

    with db_session.begin() as db:
        old_run = db.query(Run).filter_by(dataset_id=dataset_id, revision=1).one()
        run_v1_id = old_run.id
        assert old_run.superseded is True

    headers = auth(client)
    response = client.post(f'/runs/{run_v1_id}/review',
                           json={'review': 'accepted', 'decision': '安排共用打样', 'note': '尝试签核旧版'},
                           headers=headers)
    assert response.status_code == 409
    assert '取代' in response.json()['detail']


def test_viewer_cannot_register_revision(client, db_session):
    dataset_id = seed_dataset(db_session)
    headers = auth(client, 'viewer')
    response = client.post(f'/datasets/{dataset_id}/revisions',
                           json={'filename': 'x.csv', 'csv_text': csv_text(demo_rows())},
                           headers=headers)
    assert response.status_code == 403
