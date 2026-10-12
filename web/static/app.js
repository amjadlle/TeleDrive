const $=q=>document.querySelector(q), $$=q=>[...document.querySelectorAll(q)];
let config=null, authPrompt=null;
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json','X-TeleDrive-CSRF':'1',...(opt.headers||{})},...opt});if(!r.ok)throw new Error(await r.text()||r.statusText);return r.json()}
function toast(msg,bad=false){const e=$('#toast');e.textContent=msg;e.className='toast'+(bad?' bad':'');e.style.display='block';setTimeout(()=>e.style.display='none',3500)}
function fmtTs(v){return v?new Date(v*1000).toLocaleString():'-'}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

$$('.tab').forEach(b=>b.onclick=()=>{$$('.tab').forEach(x=>x.classList.remove('active'));$$('.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('#'+b.dataset.page).classList.add('active');if(b.dataset.page==='queue')loadFiles('queue');if(b.dataset.page==='history')loadFiles('history');if(b.dataset.page==='settings')loadSettings();if(b.dataset.page==='logs')loadLogs()});

async function refreshDashboard(){
try{
const d=await api('/api/dashboard');const q=d.queue||{};
$('#mPending').textContent=q.pending||0;$('#mFailed').textContent=q.failed||0;$('#mUploaded').textContent=q.uploaded||0;$('#mToday').textContent=q.uploaded_today||0;
$('#strategyView').textContent=d.strategy||'-';$('#dbView').textContent=d.database_url||'-';
const p=d.process||{};$('#processView').textContent=p.running?('running · '+(p.mode||'')):'idle';$('#subtitle').textContent=(d.strategy||'single')+' · '+(p.running?'running':'idle');
authPrompt=p.auth_prompt||null;const ap=$('#authPrompt');if(authPrompt){ap.classList.add('show');$('#authTitle').textContent=authPrompt.account+' is waiting for Telegram '+authPrompt.kind;$('#authValue').type=authPrompt.kind==='password'?'password':'text'}else{ap.classList.remove('show');$('#authValue').value=''}
$('#accounts').innerHTML=(d.accounts||[]).map(a=>'<div class="card"><div class="row"><b>'+esc(a.name)+'</b><span class="pill '+esc(a.state)+'">'+esc(a.state)+'</span></div><div class="tiny" style="margin-top:9px">enabled: '+a.enabled+' · uploaded: '+(a.uploaded_count||0)+'</div><div class="tiny">target: '+esc(a.target)+' · last success: '+fmtTs(a.last_success_ts)+'</div>'+(a.last_error?'<div style="color:var(--bad);margin-top:7px">'+esc(a.last_error)+'</div>':'')+'</div>').join('');
}catch(e){toast(e.message,true)}
}
async function loadFiles(kind){try{const rows=await api('/api/files?kind='+kind+'&limit=200');if(kind==='queue')$('#queueRows').innerHTML=rows.map(r=>'<tr><td>'+esc(r.path)+'</td><td>'+esc(r.status)+'</td><td>'+r.attempts+'</td><td>'+esc(r.tg_account||'-')+'</td><td>'+esc(r.last_error||'')+'</td></tr>').join('');else $('#historyRows').innerHTML=rows.map(r=>'<tr><td>'+esc(r.path)+'</td><td>'+esc(r.tg_account||'-')+'</td><td>'+esc(r.tg_message_id||'-')+'</td><td>'+fmtTs(r.uploaded_ts)+'</td></tr>').join('')}catch(e){toast(e.message,true)}}
async function loadLogs(){try{const x=await api('/api/logs?limit=500');$('#logBox').textContent=(x.lines||[]).join('\n');$('#logBox').scrollTop=$('#logBox').scrollHeight}catch(e){toast(e.message,true)}}

function accountEditor(a={}){return '<div class="account-edit"><div class="row"><label class="compact"><input class="ae-enabled" type="checkbox" '+(a.enabled!==false?'checked':'')+'> enabled</label><button class="btn danger compact ae-remove">Remove</button></div><div class="form-grid"><div><label>Name</label><input class="input ae-name" value="'+esc(a.name||'account')+'"></div><div><label>Phone</label><input class="input ae-phone" value="'+esc(a.phone||'')+'"></div><div><label>Session path</label><input class="input ae-session" value="'+esc(a.session_path||'/data/sessions/account')+'"></div><div><label>Target override</label><input class="input ae-target" value="'+esc(a.target??'')+'"></div><div><label>API ID override</label><input class="input ae-api-id" type="number" value="'+esc(a.api_id??'')+'"></div><div><label>API hash override</label><input class="input ae-api-hash" value="'+esc(a.api_hash??'')+'"></div></div></div>'}
function wireEditors(){$$('.ae-remove').forEach(b=>b.onclick=()=>b.closest('.account-edit').remove())}
$('#addAccount').onclick=()=>{$('#accountEditors').insertAdjacentHTML('beforeend',accountEditor({name:'account'+($$('.account-edit').length+1)}));wireEditors()}

async function loadSettings(){try{
config=await api('/api/config');const tg=config.telegram||{},up=config.upload||{},st=config.state||{},app=config.app||{};
$('#strategy').value=tg.strategy||'single';$('#apiId').value=tg.api_id||'';$('#apiHash').value=tg.api_hash||'';$('#tgTarget').value=tg.target??'me';
$('#sourceDir').value=up.source_dir||'/uploads';$('#sendMode').value=up.send_mode||'document';$('#perRun').value=up.max_files_per_run??12;$('#perDay').value=up.max_files_per_day??100;$('#sleepMin').value=up.sleep_min_seconds??60;$('#sleepMax').value=up.sleep_max_seconds??90;$('#retryAttempts').value=up.retry_attempts??5;$('#backoff').value=up.backoff_base_seconds??10;$('#floodBuffer').value=up.floodwait_buffer_seconds??5;$('#maxFileSize').value=up.max_file_size_mb??0;$('#captionTemplate').value=up.caption_template||'{name}';$('#extensions').value=(up.allowed_extensions||[]).join(',');
$('#databaseUrl').value=st.database_url||st.db_path||'sqlite:////data/state.db';$('#autoRun').checked=!!app.auto_run;$('#runInterval').value=app.run_interval_minutes??60;
$('#accountEditors').innerHTML=(tg.accounts||[]).map(accountEditor).join('');wireEditors()
}catch(e){toast(e.message,true)}}

$('#saveSettings').onclick=async()=>{try{
if(!config)await loadSettings();const tg=config.telegram=config.telegram||{},up=config.upload=config.upload||{},st=config.state=config.state||{},app=config.app=config.app||{};
tg.strategy=$('#strategy').value;tg.api_id=Number($('#apiId').value||0);tg.api_hash=$('#apiHash').value.trim();tg.target=$('#tgTarget').value.trim()||'me';
tg.accounts=$$('.account-edit').map(e=>{const o={name:e.querySelector('.ae-name').value.trim(),enabled:e.querySelector('.ae-enabled').checked,phone:e.querySelector('.ae-phone').value.trim(),session_path:e.querySelector('.ae-session').value.trim()};const target=e.querySelector('.ae-target').value.trim(),id=e.querySelector('.ae-api-id').value.trim(),hash=e.querySelector('.ae-api-hash').value.trim();if(target)o.target=target;if(id)o.api_id=Number(id);if(hash)o.api_hash=hash;return o});
up.source_dir=$('#sourceDir').value.trim();up.send_mode=$('#sendMode').value;up.max_files_per_run=Number($('#perRun').value);up.max_files_per_day=Number($('#perDay').value);up.sleep_min_seconds=Number($('#sleepMin').value);up.sleep_max_seconds=Number($('#sleepMax').value);up.retry_attempts=Number($('#retryAttempts').value);up.backoff_base_seconds=Number($('#backoff').value);up.floodwait_buffer_seconds=Number($('#floodBuffer').value);up.max_file_size_mb=Number($('#maxFileSize').value);up.caption_template=$('#captionTemplate').value;up.allowed_extensions=$('#extensions').value.split(',').map(x=>x.trim()).filter(Boolean);
st.database_url=$('#databaseUrl').value.trim()||'sqlite:////data/state.db';delete st.db_path;app.auto_run=$('#autoRun').checked;app.run_interval_minutes=Number($('#runInterval').value);
await api('/api/config',{method:'PUT',body:JSON.stringify(config)});toast('Settings saved');await refreshDashboard()
}catch(e){toast(e.message,true)}};

$('#runBtn').onclick=()=>api('/api/process/start',{method:'POST',body:JSON.stringify({mode:'run-once'})}).then(refreshDashboard).catch(e=>toast(e.message,true));
$('#scanBtn').onclick=()=>api('/api/process/start',{method:'POST',body:JSON.stringify({mode:'scan-only'})}).then(refreshDashboard).catch(e=>toast(e.message,true));
$('#stopBtn').onclick=()=>api('/api/process/stop',{method:'POST'}).then(refreshDashboard).catch(e=>toast(e.message,true));
$('#authSubmit').onclick=async()=>{if(!authPrompt)return;try{await api('/api/auth',{method:'POST',body:JSON.stringify({account:authPrompt.account,kind:authPrompt.kind,value:$('#authValue').value})});$('#authValue').value='';toast('Auth value submitted')}catch(e){toast(e.message,true)}};

refreshDashboard();setInterval(refreshDashboard,3000);
