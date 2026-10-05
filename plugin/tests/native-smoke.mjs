// Run with DSH's Electron in Node mode. This creates a unique isolated profile.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire, registerHooks } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
const root = path.resolve(process.argv[2] ?? 'D:/dsh');
const runtime = path.join(root,'resources','app.asar','dsh');
const anchor = path.join(runtime,'package.json');
const runtimeRequire = createRequire(anchor);
const packageDir = fileURLToPath(new URL('../',import.meta.url));
const local = JSON.parse(await fs.readFile(path.join(packageDir,'package.json'),'utf8'));
let resolving=false;
const hooks = registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === local.name) return {url:pathToFileURL(path.join(packageDir,'lib','index.js')).href,shortCircuit:true};
  if (specifier.startsWith('@deepseek-ai/')&&!resolving) {
    resolving=true;
    try { return {url:pathToFileURL(runtimeRequire.resolve(specifier)).href,shortCircuit:true}; }
    finally { resolving=false; }
  }
  return nextResolve(specifier,context);
}});
const {boot,readProfilePatches} = await import('@deepseek-ai/dsh-app-boot');
const {presetOperations,revision} = await import('../lib/presets.js');
const {AgentPresetRegistry} = await import('@deepseek-ai/dsh-agent-preset-registry');
const {ToolRuntime,assertObjectJsonSchema} = await import('@deepseek-ai/dsh-tools');
const yaml = runtimeRequire('js-yaml');
const scratch = mkdtempSync(path.join(tmpdir(),'dsh-preset-native-'));
const dir = path.join(scratch,'profiles','desktop'), patchPath=path.join(dir,'cordis.patch.yml'), baseFile=path.join(scratch,'base.yml');
await fs.mkdir(dir,{recursive:true});
await fs.writeFile(path.join(dir,'package.json'),JSON.stringify({name:'isolated-preset-test',private:true,dsh:{profile:{bundles:[]}}}));
await fs.writeFile(baseFile,'[]\n');
const fixturePlugin=path.join(scratch,'conditional-fixture.mjs');
await fs.writeFile(fixturePlugin,'export function apply(){if(globalThis.failPresetFixture)throw new Error("deliberate preset fixture failure");}\n');
const original={id:'source',name:'Source',order:0,plugins:[{id:'persona',name:'@deepseek-ai/dsh-persona',config:{prefix:'original\n中文\n',suffix:'working directory'},disabled:{__jsExpr:'false'}},{id:'conditional',name:pathToFileURL(fixturePlugin).href}]};
await fs.writeFile(patchPath,yaml.dump([{insert:[{id:'config-editor',name:'@deepseek-ai/dsh-config-editor'},
  {id:'preset-source',name:'@deepseek-ai/dsh-agent-preset',config:original},
  {id:local.name,name:local.name,config:{plugins:[]}}]}]));
const profile={home:scratch,dir,patchPath,installAnchor:anchor,overlays:[],name:'desktop',startedBundles:[]};
let ctx;
const routes = new Map(), logs=[];
let checks=0;
const check=(label,fn)=>{fn();checks++;console.log('PASS',label);};
async function start() {
  const current=await boot('dsh',baseFile,readProfilePatches('dsh',profile),async context=>{
    context.provide('profileContext',profile);
    context.provide('connection',{fetch:{register(options){routes.set(options.path,options);return()=>{if(routes.get(options.path)===options)routes.delete(options.path);};}}});
    context.provide('sessionProjections',{register:()=>()=>{}});
    context.provide('systemPrompt',{section:()=>()=>{},tools:()=>()=>{},getSectionOrder:()=>0});
    context.provide('sandboxPolicy',{resolve:()=>({mode:'danger-full-access'})});
    await context.plugin(ToolRuntime,{});
    await context.plugin(AgentPresetRegistry,{default:'source'});
    context.logger.exporter({levels:{default:2},export:row=>{if(row.type==='error')logs.push(row.args.map(String).join(' '));}});
  },pathToFileURL(anchor).href);
  return current;
}
function ops() {
  const entry=ctx.configEditor.entries().find(row=>row.options.name===local.name);
  assert.equal(entry?.fiber.state,2,'native editor active');
  return presetOperations(entry.fiber.ctx,entry.subtree);
}
async function api(action,args) {
  const route=routes.get('/api/'+local.name+'/'+action);assert.ok(route,'authenticated carrier route registered');
  const response=await route.fetch(new Request('http://localhost/api/'+local.name+'/'+action,{method:'POST',body:JSON.stringify(args)}));
  return {status:response.status,body:await response.json()};
}
try {
  ctx=await start();
  check('real DSH ConfigEditor and Loader active',()=>assert.ok(ctx.configEditor));
  const tool=ctx.tools.get('dsh_persona_editor');
  check('shared tool has a valid native parameter schema',()=>{assert.ok(tool);assertObjectJsonSchema(tool.parameters);});
  const toolRead=JSON.parse(await tool.execute({action:'read',id:'source'},{signal:AbortSignal.timeout(5000)}));
  check('shared Agent tool reads the same native preset',()=>assert.equal(toolRead.id,'source'));
  await assert.rejects(tool.execute({action:'read',id:'source',personaPath:['constructor',{}]},{signal:AbortSignal.timeout(5000)}));
  check('shared tool validates malformed arguments before dispatch',()=>{});
  const read=await ops().read('source');
  check('native read preserves order zero and exact text',()=>{assert.equal(read.order,0);assert.equal(read.personas[0].prefix,'original\n中文\n');});
  const live=await ctx.agentPresets.retain('source');
  const fields={id:'source',name:'Changed',description:'Example',order:0,personaPath:[0],prefix:'new\n\n  indentation\n',suffix:'tail',revision:read.revision};
  const saved=await api('save',fields);
  check('native UI API save succeeds',()=>assert.equal(saved.status,200,JSON.stringify(saved.body)));
  const updated=await ops().read('source');
  check('native save applies and preserves immutable ID',()=>{assert.equal(updated.id,'source');assert.equal(updated.name,'Changed');assert.equal(updated.personas[0].prefix,fields.prefix);});
  check('a retained running revision keeps its original persona',()=>assert.equal([...live.mount.tree.entries()][0].options.config.prefix,original.plugins[0].config.prefix));
  const stale=await api('save',fields);
  check('stale native save is refused',()=>assert.equal(stale.status,400));
  const created=await api('create',{...fields,id:'created',name:'Created',template:'source',templateRevision:updated.revision});
  check('new declared preset mounts through real DSH Loader',()=>assert.equal(created.status,200,JSON.stringify(created.body)));
  const newRead=await ops().read('created');
  check('managed native preset is editable and preserves text',()=>{assert.equal(newRead.managed,true);assert.equal(newRead.personas[0].prefix,fields.prefix);});
  const managedSave=await api('save',{...fields,id:'created',name:'Renamed display',revision:newRead.revision});
  check('managed preset saves through native config lifecycle',()=>assert.equal(managedSave.status,200,JSON.stringify(managedSave.body)));
  const toolSave=JSON.parse(await ctx.tools.get('dsh_persona_editor').execute({action:'save',...fields,id:'created',name:'Saved by tool',revision:(await ops().read('created')).revision},{signal:AbortSignal.timeout(5000)}));
  check('shared tool writes through the same native configuration service',()=>assert.equal(toolSave.ok,true));
  globalThis.failPresetFixture=true;
  const failure=await api('create',{...fields,id:'broken-copy',template:'source',templateRevision:(await ops().read('source')).revision});
  globalThis.failPresetFixture=false;
  check('native activation failure is reported',()=>assert.equal(failure.status,400));
  const afterFailure=await ops().list();
  check('failed creation rolls back only its new declaration',()=>{assert.ok(!afterFailure.presets.some(row=>row.id==='broken-copy'));assert.ok(afterFailure.presets.some(row=>row.id==='created'));});
  const persisted=yaml.load(await fs.readFile(patchPath,'utf8'),{schema:awaitSchema()});
  check('profile patch keeps !!js as a YAML expression',()=>assert.match(persisted.find(row=>row.id===local.name).config.plugins[0].config.plugins[0].disabled.__jsExpr,/false/));
  live.users--;await ctx.agentPresets.collect(live);
  await ctx.fiber.dispose();ctx=await start();
  const afterRestart=await ops().read('created');
  check('fresh native boot restores created preset and saved persona',()=>{assert.equal(afterRestart.name,'Saved by tool');assert.equal(afterRestart.personas[0].prefix,fields.prefix);});
  await assert.rejects(fs.stat(path.join(scratch,'sessions')),{code:'ENOENT'});check('no session files are created or modified',()=>{});
  check('no unexpected native activation errors',()=>assert.deepEqual(logs.filter(message=>!message.includes('deliberate preset fixture failure')),[]));
  console.log('Native checks:',checks);
} finally {
  await ctx?.fiber.dispose();hooks.deregister();
  const resolved=path.resolve(scratch),parent=path.resolve(tmpdir());
  if(path.dirname(resolved)!==parent||!path.basename(resolved).startsWith('dsh-preset-native-'))throw new Error('Unsafe fixture cleanup');
  await fs.rm(resolved,{recursive:true,force:true});
}
function awaitSchema(){return runtimeRequire('@deepseek-ai/cordis-plugin-include').entryListSchema;}
