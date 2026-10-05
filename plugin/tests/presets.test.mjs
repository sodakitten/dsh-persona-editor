import test from 'node:test';
import assert from 'node:assert/strict';
import { personaRows, updatedConfig, validateInput, templatePlugins, revision, presetOperations, PACKAGE, PRESET_PACKAGE, PERSONA_PACKAGE } from '../lib/presets.js';
const base = () => ({ id:'original', name:'Original', order:0, plugins:[
  { id:'tools', name:'group', group:true, config:[{id:'shell',name:'@deepseek-ai/dsh-tool-bash',disabled:{__jsExpr:'process.platform !== "win32"'},config:{timeout:1000}}] },
  { id:'persona', name:PERSONA_PACKAGE, config:{prefix:'old\n中文\n',suffix:'end',other:'keep'} },
] });
const fields = config => ({id:config.id,name:'New name',description:'description',order:0,personaPath:[1],prefix:'new\n  indented\n\n',suffix:'tail',revision:revision(config)});
test('editing changes only requested persona and display fields; ID, tools, expressions and whitespace survive',()=>{
  const config=base(), next=updatedConfig(config,validateInput(fields(config)));
  assert.equal(next.id,config.id);assert.equal(next.order,0);assert.deepEqual(next.plugins[0],config.plugins[0]);
  assert.equal(next.plugins[1].config.other,'keep');assert.equal(next.plugins[1].config.prefix,'new\n  indented\n\n');assert.equal(config.plugins[1].config.prefix,'old\n中文\n');
});
test('existing identity cannot be renamed',()=>assert.throws(()=>updatedConfig(base(),{...fields(base()),id:'different'}),/IDs cannot/));
test('nested personas use exact paths; unrelated personas are preserved',()=>{
  const config=base();config.plugins[0].config.push({id:'nested',name:PERSONA_PACKAGE,config:{prefix:'nested'}});
  assert.deepEqual(personaRows(config.plugins).map(x=>x.path),[[0,'config',1],[1]]);
  const next=updatedConfig(config,{...fields(config),personaPath:[0,'config',1]});
  assert.equal(next.plugins[0].config[1].config.prefix,'new\n  indented\n\n');assert.deepEqual(next.plugins[1],config.plugins[1]);
});
test('expressions and forged paths are refused instead of erased',()=>{
  const config=base();config.plugins[1].config.prefix={__jsExpr:'ctx.value'};
  assert.throws(()=>updatedConfig(config,fields(config)),/not editable/);
  for(const personaPath of [[1,'__proto__',0],['constructor'],[1,'config','prototype'],[-1]])assert.throws(()=>validateInput({...fields(base()),personaPath}));
});
test('persona without a config can gain text; add uses unique row id',()=>{
  const config=base();delete config.plugins[1].config;
  assert.equal(updatedConfig(config,fields(config)).plugins[1].config.prefix,'new\n  indented\n\n');
  config.plugins[1]={id:'persona',name:'other'};
  const added=updatedConfig(config,{...fields(config),personaPath:null});assert.equal(added.plugins[2].id,'persona-2');
  assert.throws(()=>updatedConfig(base(),{...fields(base()),personaPath:null}),/Choose/);
});
test('template keeps raw !!js and resolves relative plugin modules at source',()=>{
  const rows=[{id:'group',name:'group',group:true,config:[{id:'relative',name:'./tool.js',config:{value:{__jsExpr:'ctx.foo'}}}]}];
  assert.equal(templatePlugins(rows,'file:///C:/source/main.js')[0].config[0].name,'file:///C:/source/tool.js');
  assert.equal(rows[0].config[0].name,'./tool.js');assert.deepEqual(templatePlugins(rows,'file:///C:/source/main.js')[0].config[0].config,rows[0].config[0].config);
});
test('validation rejects blank names, invalid IDs, oversized prompts, bad order and missing revision',()=>{
  for(const edit of [{id:'Bad'}, {id:'../bad'}, {id:'-bad'}, {name:' '}, {order:'not a number'}, {order:Infinity}, {revision:''}, {prefix:'x'.repeat(1048577)}])assert.throws(()=>validateInput({...fields(base()),...edit}));
  assert.equal(validateInput({...fields(base()),order:''}).order,undefined);
});
function fixture() {
  const fiber={}, entry={options:{id:'preset-original',name:PRESET_PACKAGE,config:base()},ctx:{baseUrl:'file:///C:/source/main.js'}},
    editor={options:{id:PACKAGE,name:PACKAGE,config:{plugins:[]}},fiber,ctx:{baseUrl:'file:///C:/editor/index.js'}};
  let writes=0;
  const ctx={fiber,baseUrl:editor.ctx.baseUrl,configEditor:{entries:()=>[entry,editor],edit:async(target,change)=>{target.options.config=change(structuredClone(target.options.config));writes++;}},
    agentPresets:{remoteExportList:async()=>({presets:[{id:'original',isDefault:true},...editor.options.config.plugins.map(row=>({id:row.config.id,isDefault:false}))]}),
      list:async()=>[...editor.options.config.plugins.map(row=>({id:row.config.id}))]},};
  return {ctx,entry,editor,get writes(){return writes;},ops:presetOperations(ctx,{})};
}
test('API writes through configEditor and detects concurrent modification without a write',async()=>{
  const f=fixture(), value=await f.ops.read('original');assert.equal(value.order,0);
  await f.ops.save({...fields(base()),revision:value.revision});assert.equal(f.writes,1);assert.equal(f.entry.options.config.id,'original');
  await assert.rejects(f.ops.save({...fields(base()),revision:value.revision}),/changed/);assert.equal(f.writes,1);
});
test('create copies current template, persists native declaration, rejects stale templates and duplicates',async()=>{
  const f=fixture(), value=await f.ops.read('original'), raw={...fields(base()),id:'new-preset',template:'original',templateRevision:value.revision};
  await f.ops.create(raw);assert.equal(f.writes,1);assert.equal(f.editor.options.config.plugins[0].name,PRESET_PACKAGE);
  assert.deepEqual(f.editor.options.config.plugins[0].config.plugins[0],base().plugins[0]);assert.deepEqual(f.entry.options.config,base());
  await assert.rejects(f.ops.create(raw),/already exists/);assert.equal(f.writes,1);
  await assert.rejects(f.ops.create({...raw,id:'other',templateRevision:'0'.repeat(64)}),/changed/);assert.equal(f.writes,1);
  const newValue=await f.ops.read('new-preset');await f.ops.save({...fields({...base(),id:'new-preset'}),revision:newValue.revision});assert.equal(f.writes,2);
});

test('template revision is checked again inside the configuration lock',async()=>{
  const f=fixture(), value=await f.ops.read('original');
  const list=f.ctx.agentPresets.remoteExportList;
  f.ctx.agentPresets.remoteExportList=async()=>{const result=await list();f.entry.options.config.name='Changed concurrently';return result;};
  await assert.rejects(f.ops.create({...fields(base()),id:'raced',template:'original',templateRevision:value.revision}),/Template changed/);
  assert.equal(f.writes,0);assert.deepEqual(f.editor.options.config.plugins,[]);
});

test('failed activation rolls back its declaration while preserving existing managed presets',async()=>{
  const f=fixture(), value=await f.ops.read('original');
  await f.ops.create({...fields(base()),id:'existing',template:'original',templateRevision:value.revision});
  f.ctx.agentPresets.list=async()=>f.editor.options.config.plugins.map(row=>({id:row.config.id,broken:row.config.id==='failed'?'Activation failed':undefined}));
  await assert.rejects(f.ops.create({...fields(base()),id:'failed',template:'original',templateRevision:value.revision}),/Activation failed/);
  assert.deepEqual(f.editor.options.config.plugins.map(row=>row.config.id),['existing']);
});
