import assert from 'node:assert/strict';
const calls=[];
globalThis.fetch=async(path,options)=>{calls.push(JSON.parse(options.body));return {ok:true,json:async()=>({saved:true})};};
const {save}=await import('../mtanalog/static/private.js');
save('FIRSTUSDT',{step:1});save('SECONDUSDT',{step:2});save('FIRSTUSDT',{step:3});
await new Promise(resolve=>setTimeout(resolve,750));
assert.equal(calls.length,2);assert.equal(calls.find(c=>c.symbol==='FIRSTUSDT').settings.step,3);
assert.equal(calls.find(c=>c.symbol==='SECONDUSDT').settings.step,2);
console.log('Private settings remain independent when switching pairs during debounce.');
