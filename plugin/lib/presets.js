import { createHash } from 'node:crypto';

export const PACKAGE = 'dsh-agent-preset-editor';
export const PRESET_PACKAGE = '@deepseek-ai/dsh-agent-preset';
export const PERSONA_PACKAGE = '@deepseek-ai/dsh-persona';
export const ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;
export const clone = value => structuredClone(value);
export const revision = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

export function personaRows(plugins, prefix = []) {
  if (!Array.isArray(plugins)) throw new Error('Invalid preset plugin list');
  return plugins.flatMap((row, index) => {
    if (!object(row)) throw new Error('Invalid preset plugin row');
    const path = [...prefix, index];
    if (row.name === PERSONA_PACKAGE) {
      if (!object(row.config ?? {})) throw new Error('Persona configuration is not editable');
      return [{ path, id: row.id ?? String(index + 1),
        prefix: typeof row.config?.prefix === 'string' ? row.config.prefix : '',
        suffix: typeof row.config?.suffix === 'string' ? row.config.suffix : '',
        editable: (row.config?.prefix === undefined || typeof row.config.prefix === 'string') &&
          (row.config?.suffix === undefined || typeof row.config.suffix === 'string'),
      }];
    }
    return row.group === true ? personaRows(row.config, [...path, 'config']) : [];
  });
}

function text(value, field, max) {
  if (typeof value !== 'string' || value.length > max || value.includes('\0')) throw new Error(`Invalid ${field}`);
  return value;
}
export function validateInput(input, creating = false) {
  if (!object(input)) throw new Error('Expected preset fields');
  if (typeof input.id !== 'string' || input.id.length > 80 || !ID.test(input.id)) throw new Error('Invalid preset ID');
  const out = { id: input.id, name: text(input.name, 'name', 200).trim(),
    description: text(input.description, 'description', 4000).trim(),
    prefix: text(input.prefix, 'prefix', 1024 * 1024), suffix: text(input.suffix, 'suffix', 1024 * 1024) };
  if (!out.name) throw new Error('Preset name is required');
  out.order = input.order === '' || input.order === null || input.order === undefined ? undefined : Number(input.order);
  if (out.order !== undefined && (typeof input.order !== 'number' && typeof input.order !== 'string' || !Number.isFinite(out.order))) throw new Error('Invalid preset order');
  out.personaPath = input.personaPath ?? null;
  if (out.personaPath !== null && (!Array.isArray(out.personaPath) || out.personaPath.length > 64 || out.personaPath.some((part, i) => i % 2 === 0 ? !Number.isSafeInteger(part) || part < 0 : part !== 'config'))) throw new Error('Invalid persona path');
  if (!creating && (typeof input.revision !== 'string' || !/^[a-f0-9]{64}$/u.test(input.revision))) throw new Error('Reload this preset before saving');
  return { ...out, revision: input.revision };
}

export function updatedConfig(original, input) {
  const next = clone(original);
  if (next.id !== input.id) throw new Error('Preset IDs cannot be changed; create a new preset instead');
  next.name = input.name;
  if (input.description) next.description = input.description; else delete next.description;
  if (input.order !== undefined) next.order = input.order; else delete next.order;
  const personas = personaRows(next.plugins);
  if (input.personaPath === null) {
    if (personas.length) throw new Error('Choose the persona to edit');
    if (input.prefix || input.suffix) {
      let id = 'persona', counter = 1;
      const ids = new Set(next.plugins.map(row => row.id));
      while (ids.has(id)) id = 'persona-' + (++counter);
      next.plugins.push({ id, name: PERSONA_PACKAGE, config: { prefix: input.prefix, suffix: input.suffix } });
    }
  } else {
    const selected = personas.find(row => JSON.stringify(row.path) === JSON.stringify(input.personaPath));
    if (!selected?.editable) throw new Error('This persona is not editable');
    let row = next.plugins;
    for (const part of selected.path) row = row[part];
    row.config = { ...(row.config ?? {}), prefix: input.prefix, suffix: input.suffix };
  }
  return next;
}

/** Resolve relative module names at the original declaration, preserving all other fields and !!js data. */
export function templatePlugins(rows, baseUrl) {
  const plugins = clone(rows);
  function walk(entries) {
    for (const row of entries) {
      if (typeof row.name !== 'string' || !row.name) throw new Error('Invalid template plugin');
      if (/^\.{1,2}\//u.test(row.name)) row.name = new URL(row.name, baseUrl).href;
      if (row.group === true) walk(row.config);
    }
  }
  walk(plugins);
  return plugins;
}

/** Only public configuration/registry services are used. No session files or profile files are written here. */
export function presetOperations(ctx) {
  function owner() {
    const entry = ctx.configEditor.entries().find(row => row.options.name === PACKAGE && row.fiber === ctx.fiber);
    if (!entry) throw new Error('Preset editor is unavailable; reopen it after reload');
    return entry;
  }
  function locate(id) {
    const roots = ctx.configEditor.entries().filter(row => row.options.name === PRESET_PACKAGE && row.options.config?.id === id);
    const own = (owner().options.config?.plugins ?? []).filter(row => row.name === PRESET_PACKAGE && row.config?.id === id);
    if (roots.length + own.length !== 1) throw new Error('This preset is not uniquely editable in this profile');
    return roots.length ? { entry: roots[0], config: roots[0].options.config, managed: false, baseUrl: roots[0].ctx.baseUrl }
      : { entry: owner(), row: own[0], config: own[0].config, managed: true, baseUrl: ctx.baseUrl };
  }
  async function list() {
    return { presets: (await ctx.agentPresets.remoteExportList()).presets.map(row => {
      try { locate(row.id); return { ...row, editable: true }; }
      catch { return { ...row, editable: false }; }
    }) };
  }
  async function read(id) {
    const found = locate(id);
    return { id, name: found.config.name ?? id, description: found.config.description ?? '',
      order: found.config.order ?? '', revision: revision(found.config), personas: personaRows(found.config.plugins), managed: found.managed };
  }
  async function save(raw) {
    const input = validateInput(raw), found = locate(input.id);
    await ctx.configEditor.edit(found.entry, current => {
      if (!found.managed) {
        if (revision(current) !== input.revision) throw new Error('Preset changed in another editor; reload before saving');
        return updatedConfig(current, input);
      }
      const rows = current.plugins ?? [], row = rows.find(row => row.name === PRESET_PACKAGE && row.config?.id === input.id);
      if (!row || revision(row.config) !== input.revision) throw new Error('Preset changed in another editor; reload before saving');
      return { ...current, plugins: rows.map(other => other === row ? { ...row, config: updatedConfig(row.config, input) } : other) };
    });
    return { ok: true, id: input.id };
  }
  async function create(raw) {
    const input = validateInput(raw, true);
    if (typeof raw.template !== 'string') throw new Error('Choose an existing template');
    const source = locate(raw.template);
    if (!raw.templateRevision || revision(source.config) !== raw.templateRevision) throw new Error('Template changed; reload before creating');
    const config = updatedConfig({ ...clone(source.config), id: input.id, plugins: templatePlugins(source.config.plugins, source.baseUrl) }, input);
    const rows = (await list()).presets;
    if (rows.some(row => row.id === input.id)) throw new Error('Preset ID already exists');
    if (rows.find(row => row.id === raw.template)?.broken) throw new Error('The template has an activation error');
    const owningEntry = owner();
    await ctx.configEditor.edit(owningEntry, current => {
      // configEditor holds the same profile lock as other editors and plugin management.
      const currentSource = source.managed
        ? (current.plugins ?? []).find(row => row.name === PRESET_PACKAGE && row.config?.id === raw.template)?.config
        : ctx.configEditor.entries().find(row => row.options.id === source.entry.options.id && row.options.name === PRESET_PACKAGE)?.options.config;
      if (!currentSource || revision(currentSource) !== raw.templateRevision) throw new Error('Template changed; reload before creating');
      if ((current.plugins ?? []).some(row => row.config?.id === input.id) ||
          ctx.configEditor.entries().some(row => row.options.name === PRESET_PACKAGE && row.options.config?.id === input.id)) throw new Error('Preset ID already exists');
      return { ...current, plugins: [...(current.plugins ?? []), { id: 'preset-' + input.id, name: PRESET_PACKAGE, config }] };
    });
    const mounted = (await ctx.agentPresets.list()).find(row => row.id === input.id);
    if (!mounted || mounted.broken) {
      const nextOwner=ctx.configEditor.entries().find(row=>row.options.id===owningEntry.options.id && row.options.name===PACKAGE);
      if (nextOwner) await ctx.configEditor.edit(nextOwner,current=>{
        const created=(current.plugins??[]).find(row=>row.name===PRESET_PACKAGE&&row.config?.id===input.id);
        if (!created || revision(created.config)!==revision(config)) throw new Error('Preset changed during activation; it was retained for inspection');
        return {...current,plugins:current.plugins.filter(row=>row!==created)};
      });
      throw new Error(mounted?.broken ?? 'Preset could not be activated');
    }
    return { ok: true, id: input.id };
  }
  return { list, read, save, create };
}
