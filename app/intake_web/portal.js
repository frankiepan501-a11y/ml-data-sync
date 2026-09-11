'use strict';
const base = '/report/ml-intake/test';
const grant = new URLSearchParams(location.hash.slice(1)).get('token') || '';
history.replaceState(null, '', location.pathname);
const el = id => document.getElementById(id);
const states = {queued:'已保存，等待读取',processing:'正在读取',parsed:'费用明细已读取',needs_review:'已收，待核实',failed:'读取失败（原件保留）'};
const kinds = {charges:'费用明细',period:'官方账期/单据清单',support:'补充证明'};
const sources = {historical_official:'历史官方原件（提交人声明）',synthetic:'构造测试样本',unverified:'待核实'};
async function api(path, options={}) {
  const r = await fetch(base + path, {...options, headers:{...options.headers,Authorization:'Bearer ' + grant},cache:'no-store',referrerPolicy:'no-referrer'});
  if (!r.ok) { const v = await r.json().catch(()=>({})); throw new Error(typeof v.detail==='string'?v.detail:'请求未完成，请重试'); }
  return r;
}
function text(tag, content) { const n=document.createElement(tag); n.textContent=content; return n; }
async function refresh() {
  const data=await (await api('/status')).json();
  el('files').replaceChildren();
  if (!data.files.length) el('files').append(text('li','尚未上传文件。'));
  for (const f of data.files) {
    const seller=data.sellers.find(s=>s.id===f.seller);
    const li=text('li',`${seller.label} · ${f.name} · ${states[f.state]||f.state}\n文件编号：${f.id}\n提交时间：${new Date(f.created*1000).toLocaleString()}\n${f.result.reason||''}`);
    li.style.whiteSpace='pre-wrap';
    li.append(text('p',`用途：${kinds[f.kind]||f.kind}；大小：${(f.size/1024).toFixed(1)} KB`));
    li.append(text('p','测试材料来源：'+(sources[f.source]||'待核实')+'。此分类不代表核销通过。'));
    if(f.result.month_counts) li.append(text('p','已读取费用行数：'+f.result.rows+'；按费用月份：'+Object.entries(f.result.month_counts).map(([m,n])=>`${m} ${n}行`).join('、')+'。这不代表核销通过。'));
    if (f.same_name_conflict) li.append(text('strong','\n同名文件内容不同，两份均已保留，请核对。'));
    const button=text('button','下载原件');button.type='button';button.onclick=async()=>{
      try { const blob=await (await api('/files/'+f.id)).blob(); const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=f.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); }
      catch(e){el('message').textContent=e.message;}
    };li.append(document.createElement('br'),button);el('files').append(li);
  }
  el('checks').replaceChildren();
  for(const s of data.sellers){const p=text('p',`${s.label}：${s.missing.length?'尚未上传 '+s.missing.join('、'):'两类材料均已收存，尚未核实齐全'}。${s.checks_pending.join('；')}。`);el('checks').append(p);}
}
el('form').onsubmit=async e=>{
  e.preventDefault();const f=el('file').files[0];if(!f)return;
  if(f.size>20*1024*1024){el('message').textContent='文件超过20MB';return;}
  el('send').disabled=true;el('message').textContent='正在上传，请不要关闭页面…';
  try {const q=new URLSearchParams({seller:el('seller').value,kind:el('kind').value,source:el('source').value,name:f.name});const v=await(await api('/files?'+q,{method:'POST',body:f})).json();el('message').textContent=(v.duplicate?'已收到相同原件，未重复保存。':'原件已保存，正在排队检查。')+' 文件编号：'+v.id;await refresh();}
  catch(e){el('message').textContent=e.message;}finally{el('send').disabled=false;}
};
el('refresh').onclick=()=>refresh().catch(e=>el('message').textContent=e.message);
if(!grant){el('send').disabled=true;el('message').textContent='请从财务助手提交卡重新打开，当前页面没有有效上传凭证。';}
else{refresh().catch(e=>el('message').textContent=e.message);setInterval(()=>{if(!document.hidden)refresh().catch(()=>{});},10000);}
