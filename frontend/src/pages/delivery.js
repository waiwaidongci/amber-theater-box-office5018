import {feedback} from '../components/common/fields.js';
import {panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';
import {runPage} from './run.js';

function versionCell(row) {
  if (!row.publication_id) return pill('未发布', 'unpublished');
  return el('span', {}, `版本 #${row.publication_id}`);
}

export async function deliveryPage(route, refresh) {
  const {spec, runs} = workspace;
  const message = feedback();
  const artifacts = table([
    {label:'报告', render:row=>link(row.name,`#results/${row.id}`)},
    {label:'发布版本', render:versionCell},
    {label:'营业日', render:row=>row.report_day || '—'},
    {label:'复核', render:row=>pill(REVIEW[row.review],row.review)},
    {label:'计算时间', render:row=>date(row.finished_at)},
    {label:'下载', render:row=>{
      const download = button('下载 CSV',{class:'quiet',disabled:!row.has_export});
      download.addEventListener('click', handler(download,message,() =>
        workspace.download(`/runs/${row.id}/export`,`${spec.short_name}-${row.id}.csv`)));
      return download;
    }},
  ], runs.filter(row=>row.status==='complete'), {caption:'可导出的结果（含已被替代但已签收的历史版本）', empty:'完成计算并通过一致性发布后会生成对应的报告。'});
  const delivery = spec.scheduled ? panel('站内分发回执', table([
    {key:'recipient',label:'收件团队'},
    {label:'回执',render:row=>pill(row.state==='delivered'?'已投递':'已拦截',row.state)},
    {key:'detail',label:'说明'}, {label:'时间',render:row=>date(row.created_at)},
    {label:'查看',render:row=>link(`运行 ${row.run_id}`,`#results/${row.run_id}`)},
  ],await workspace.deliveries(),{caption:'站内收件箱（签收记录随运行永久保留）',empty:'计划到时执行，回执将在此更新。'})) : null;
  return el('div',{}, panel(spec.pages[5], p('按发布版本下载 CSV；重算切换不影响已签收日报，复核状态随历史报告保留。','subtle'), artifacts, message),
    delivery, spec.scheduled && await runPage({...route,scheduling:true},refresh));
}
