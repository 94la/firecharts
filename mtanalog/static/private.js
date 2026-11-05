// Loaded only on the owner's /private page. Every data/control API enforces the session.
let selectHandler=()=>{},current=null,pairs=[],lastSaved=new Map();
const pending=new Map(),inFlight=new Map();
const $=id=>document.getElementById(id);
async function request(path,value){
 const response=await fetch('/api/private/'+path,{method:value===undefined?'GET':'POST',cache:'no-store',credentials:'same-origin',headers:value===undefined?{}:{'Content-Type':'application/json'},body:value===undefined?undefined:JSON.stringify(value),signal:AbortSignal.timeout(20000)});
 const data=await response.json();if(!response.ok){const failure=Error(data.error||'Не удалось выполнить запрос.');failure.status=response.status;throw failure;}return data;
}
function error(message){$('private-error').textContent=message;$('private-error').hidden=!message;}
function render(){
 const list=$('private-list');list.replaceChildren();
 const names={starting:'запускается',synchronizing:'синхронизация',collecting:'сбор идёт',reconnecting:'переподключение',stopped:'ожидает перезапуска',deleting:'удаление…'};
 for(const item of pairs){
  const row=document.createElement('div');row.className='private-pair';
  const info=document.createElement('span');const label=document.createElement('strong');label.textContent=item.base_asset+' / '+item.quote_asset;
  const meta=document.createElement('small');const minutes=item.first_ms==null?0:Math.max(0,Math.round((item.last_ms-item.first_ms)/60000));
  meta.textContent=(names[item.state]||item.state)+' · '+minutes+' мин · '+(item.bytes/1048576).toFixed(1)+' МБ';info.append(label,meta);
  const open=document.createElement('button');open.className='button';open.textContent=current===item.symbol?'Открыта':'Открыть';open.disabled=item.state==='deleting';
  open.addEventListener('click',()=>{current=item.symbol;selectHandler(item);render();});
  const remove=document.createElement('button');remove.className='button private-delete';remove.textContent='Остановить и удалить';remove.disabled=item.state==='deleting';
  remove.addEventListener('click',async()=>{remove.disabled=true;try{try{await flushPending(item.symbol);}catch(e){error(e.message);}await request('delete',{symbol:item.symbol});if(current===item.symbol){current=null;selectHandler(null);}await refresh();}catch(e){error(e.message);remove.disabled=false;}});
  row.append(info,open,remove);list.append(row);
 }
 if(!pairs.length){const p=document.createElement('p');p.className='helper';p.textContent='Выберите пару и запустите сбор. История начнёт появляться через несколько секунд.';list.append(p);}
}
async function refresh(){
 const value=await request('pairs');pairs=value.pairs;
 $('private-limit').textContent='Последние 6 часов · до '+value.max_active+' личных пар одновременно · публичный BTCUSDT защищён';
 render();
}
export function onSelect(handler){selectHandler=handler;}
async function flushPending(symbol){
 const item=pending.get(symbol);
 if(!item)return inFlight.get(symbol);
 clearTimeout(item.timer);pending.delete(symbol);
 const job=(inFlight.get(symbol)||Promise.resolve()).catch(()=>{}).then(async()=>{
  await request('settings',{symbol,settings:item.settings});lastSaved.set(symbol,item.serialized);
 });
 inFlight.set(symbol,job);
 try{await job;}finally{if(inFlight.get(symbol)===job)inFlight.delete(symbol);}
}
export function save(symbol,settings){
 const serialized=JSON.stringify(settings);
 if(lastSaved.get(symbol)===serialized&&!pending.has(symbol)&&!inFlight.has(symbol))return;
 clearTimeout(pending.get(symbol)?.timer);
 const item={settings:structuredClone(settings),serialized,timer:null};
 item.timer=setTimeout(()=>flushPending(symbol).catch(e=>error(e.message)),500);pending.set(symbol,item);
}
export async function init(){
 // Do not construct or display owner controls before the server verifies the session.
 try{
  const key=new URLSearchParams(location.hash.slice(1)).get('key');
  if(key){history.replaceState(null,'',location.pathname+location.search);await request('session',{token:key});}
  await request('pairs');
 }catch(e){
  if(e.status===403)location.replace('/');
  return;
 }
 const panel=document.createElement('section');panel.className='private-panel glass';panel.id='private-panel';
 panel.innerHTML='<div class="settings-heading"><h2>Личные пары</h2><button id="private-btc" class="button">Публичный BTCUSDT</button></div><p id="private-limit" class="helper">История за последние 6 часов</p><div class="private-add"><input id="private-symbol" list="private-catalog" placeholder="Пара, например ETHUSDT или ETHBTC" autocomplete="off" aria-label="Пара Binance Spot"><datalist id="private-catalog"></datalist><button id="private-start" class="button price-apply" disabled>Начать сбор</button></div><div id="private-list"></div><p id="private-error" class="error" hidden></p>';
 document.querySelector('.market-heading').before(panel);
 $('private-btc').addEventListener('click',()=>{current=null;selectHandler(null);render();});
 try{
  await refresh();
 }catch(e){error(e.message+' Откройте личную ссылку владельца.');return;}
 const catalogue=new Map();
 $('private-start').addEventListener('click',async()=>{
  const symbol=$('private-symbol').value.trim().toUpperCase().replace('/','');
  if(!catalogue.has(symbol)){error('Выберите пару из списка Binance Spot.');return;}
  $('private-start').disabled=true;error('');
  try{const result=await request('start',{symbol});pairs=result.pairs;const item=pairs.find(p=>p.symbol===symbol);current=symbol;selectHandler(item);render();}
  catch(e){error(e.message);}finally{$('private-start').disabled=false;}
 });
 request('catalog').then(value=>{
  for(const item of value.symbols.sort((a,b)=>a.symbol.localeCompare(b.symbol))){
   if(item.symbol==='BTCUSDT')continue;catalogue.set(item.symbol,item);
   const option=document.createElement('option');option.value=item.symbol;option.label=item.base_asset+' / '+item.quote_asset;$('private-catalog').append(option);
  }
  $('private-start').disabled=false;
 }).catch(e=>error(e.message+' Обновите страницу для повторной загрузки списка пар.'));
 setInterval(()=>{if(!document.hidden)refresh().catch(e=>{if(e.status===403){panel.remove();location.replace('/');}else error(e.message);});},5000);
}
