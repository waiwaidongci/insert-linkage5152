import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

export async function inputs(route, refresh) {
  const {spec,datasets} = workspace;
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  const error = feedback();
  const sample = button('下载示例 CSV', {class:'quiet',onClick:event=>handler(sample,error,()=>workspace.download('/template',spec.sample_name))(event)});
  const detail = selected && await workspace.getDataset(selected.id,route.offset);
  const columns = spec.fields.map(field => ({key:field.key,label:field.label}));
  const lineage = detail ? await workspace.revisions(detail.id) : [];
  const revisionTable = panel('批次更正版本',
    p('在原批次上登记修正后的适配特征，每版只重算至少一端较前版有改动的规格对，切树与整批重算一致。','subtle'),
    table([
      {key:'revision',label:'版本',render:row=>`第 ${row.revision} 版`},
      {label:'状态',render:row=>row.published ? pill('已发布','published') : pill('待发布','pending')},
      {key:'row_count',label:'规格数'},
      {key:'author',label:'登记人'},
      {label:'登记时间',render:row=>date(row.created_at)},
      {label:'重算记录',render:row=>row.runs.length
        ? el('span',{}, ...row.runs.flatMap((run,index)=>[index ? '、' : '', link(`运行 ${run.id}`,`#results/${run.id}`)]))
        : p('暂无','subtle')},
    ],lineage,{caption:'版本链',empty:'暂无更正版本。'}));
  const correctionButton = button('登记下一版更正',{type:'submit',class:'primary','data-testid':'register-revision'});
  const correction = session.canWrite && detail && el('form',{class:'fields',onSubmit:handler(correctionButton,error,async event=>{
    const data = new FormData(event.target);
    const file = data.get('correction');
    if (!file?.size) throw new Error('请选择修正后的 CSV 文件');
    if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
    await workspace.registerRevision(detail.id,{filename:file.name,csv_text:await file.text()});
    await refresh();
    location.hash = `#run/${detail.id}`;
  })},el('label',{class:'field',for:'correction-source'}, el('span',{},'修正后的完整台账 CSV'),
      el('input',{id:'correction-source',type:'file',name:'correction',accept:'.csv,text/csv',required:true})),
    correctionButton, error);
  const preview = detail ? panel('批次明细', el('div',{class:'actions'},
    el('label',{for:'dataset-id',class:'sr'}, '选择数据批次'),
    el('select',{id:'dataset-id','aria-label':'选择数据批次',onChange:event=>{location.hash=`#inputs/${event.target.value}`;}},
      datasets.map(item=>el('option',{value:item.id,selected:item.id === selected.id}, item.name))),
    link('使用此批次运行',`#run/${detail.id}`,{class:'quiet'})),
    p(`${detail.filename} · ${detail.row_count} 行 · ${date(detail.created_at)}`,'filename'),
    table(columns,detail.rows,{caption:'批次明细',pageSize:16}),
    el('div',{class:'pagination'},
      route.offset > 0 && link('上一批明细',`#inputs/${detail.id}/${Math.max(0,route.offset-16)}`,{class:'quiet'}),
      p(`${detail.rows.length ? route.offset+1 : route.offset}–${route.offset+detail.rows.length} / ${detail.row_count}`),
      route.offset+16 < detail.row_count && link('下一批明细',`#inputs/${detail.id}/${route.offset+16}`,{class:'quiet'})))
    : empty('没有可用的批次。请导入示例或业务 CSV。');
  const submit = button('校验并导入', {type:'submit',class:'primary'});
  const upload = session.canWrite && el('form',{class:'fields',onSubmit:handler(submit,error,async event=>{
    const data = new FormData(event.target);
    const file = data.get('source');
    if (!file?.size) throw new Error('请选择一个 CSV 文件');
    if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
    const item = await workspace.upload({name:data.get('batch_name'),filename:file.name,csv_text:await file.text()});
    const target = `#inputs/${item.id}`;
    if (location.hash === target) await refresh();
    else location.hash = target;
  })},field({key:'batch_name',label:'批次名称',type:'string',max_length:120}),
    el('label',{class:'field',for:'source'}, el('span',{},'业务 CSV'),el('input',{id:'source',type:'file',name:'source',accept:'.csv,text/csv',required:true})), submit);
  return el('div',{class:'two-col'},el('div',{class:'wide-panel'},preview,revisionTable,
    correction && panel('登记批次更正',p('前版未算完也可登记，多版可并发重算；只有更新版能发布，晚完成的旧版会标记为被取代。','subtle'),correction)),
    el('aside',{class:'sidebar-panel'},
    panel('数据导入',p('导入后保留原批次和校验指纹，可用同一份数据复跑。','subtle'),sample,upload,error),
    panel('字段说明',el('dl',{class:'dictionary'},spec.fields.map(item=>el('div',{},el('dt',{},item.label),
      el('dd',{},item.key,item.required === false ? ' · 可空' : ' · 必填',item.options ? ` · ${item.options.join(' / ')}` : '')))))));
}
