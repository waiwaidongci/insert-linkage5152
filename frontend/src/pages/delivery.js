import {feedback} from '../components/common/fields.js';
import {panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';
import {runPage} from './run.js';

export async function deliveryPage(route, refresh) {
  const {spec, runs, datasets} = workspace;
  const message = feedback();
  const nameOf = datasetId => datasets.find(item => item.id === datasetId)?.name || `批次 ${datasetId}`;
  const completed = runs.filter(row => row.status === 'complete');

  // 每个批次的已发布最新版本由后端发布指针标记（run.published）。
  const latestByDataset = new Map();
  for (const run of completed) {
    if (!run.published) continue;
    const current = latestByDataset.get(run.dataset_id);
    if (!current || (run.revision || 1) > (current.revision || 1)) {
      latestByDataset.set(run.dataset_id, run);
    }
  }
  const latestRows = [...latestByDataset.entries()].map(([datasetId, run]) => ({datasetId, run}));

  const latest = table([
    {label:'数据批次', render:row=>link(nameOf(row.datasetId),`#inputs/${row.datasetId}`)},
    {label:'最新发布版本', render:row=>`第 ${row.run.revision || 1} 版（运行 ${row.run.id}）`},
    {label:'计算时间', render:row=>date(row.run.finished_at)},
    {label:'下载', render:row=>{
      const download = button('下载最新完整版本 CSV',{class:'primary','data-testid':'latest-export'});
      download.addEventListener('click', handler(download,message,() =>
        workspace.download(`/datasets/${row.datasetId}/latest-export`,
          `${spec.short_name}-${row.datasetId}-r${row.run.revision || 1}.csv`)));
      return download;
    }},
  ], latestRows, {caption:'按批次下载最新发布版本', empty:'完成并发布版本后，采购在此下载最新完整清单。'});

  const artifacts = table([
    {label:'报告', render:row=>link(row.name,`#results/${row.id}`)},
    {label:'版本', render:row=>row.revision ? `第 ${row.revision} 版` : '—'},
    {label:'复核', render:row=>pill(REVIEW[row.review],row.review)},
    {label:'计算时间', render:row=>date(row.finished_at)},
    {label:'签核文件', render:row=>{
      const download = button('下载原签核 CSV',{class:'quiet'});
      download.addEventListener('click', handler(download,message,() =>
        workspace.download(`/runs/${row.id}/export`,`${spec.short_name}-${row.id}.csv`)));
      return el('span',{},download, row.superseded && pill('已被新版取代','superseded'));
    }},
  ], completed, {caption:'各运行原签核文件（照旧保留）', empty:'完成计算后会生成对应的报告。'});
  const delivery = spec.scheduled ? panel('站内分发回执', table([
    {key:'recipient',label:'收件团队'},
    {label:'回执',render:row=>pill(row.state==='delivered'?'已投递':'已拦截',row.state)},
    {key:'detail',label:'说明'}, {label:'时间',render:row=>date(row.created_at)},
    {label:'查看',render:row=>link(`运行 ${row.run_id}`,`#results/${row.run_id}`)},
  ],await workspace.deliveries(),{caption:'站内收件箱',empty:'计划到时执行，回执将在此更新。'})) : null;
  return el('div',{}, panel(spec.pages[5],
    p('采购按批次下载已发布的最新完整版本；各运行的原签核文件照旧保存在下方，可随时回溯。','subtle'),
    latest, message),
    panel('历史运行与签核文件', artifacts),
    delivery, spec.scheduled && await runPage({...route,scheduling:true},refresh));
}
