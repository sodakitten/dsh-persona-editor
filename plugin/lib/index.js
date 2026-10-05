import z from '@deepseek-ai/schemastery';
import { Service } from '@deepseek-ai/cordis';
import { EntryGroup, EntryTree } from '@deepseek-ai/cordis-plugin-loader';
import { approveEscalation } from '@deepseek-ai/dsh-sandbox';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { presetOperations, PRESET_PACKAGE } from './presets.js';

export default class PersonaEditor {
  static inject = ['connection', 'configEditor', 'agentPresets', 'loader'];
  // Retain raw !!js conditions when the declaration is copied from another preset.
  static [EntryGroup.key] = true;
  static Config = z.object({ plugins: z.array(z.any()).default([]) });
  constructor(ctx, config) { this.ctx = ctx; this.config = config; }
  async *[Service.init]() {
    const { ctx, config } = this;
    if (config.plugins.some(row => row.name !== PRESET_PACKAGE)) throw new Error('Editor children must be Agent preset declarations');
    const tree = new EntryTree(ctx);
    ctx.effect(() => () => tree.root.stop(), 'persona-editor: owned preset declarations');
    await tree.root.update(config.plugins);
    await tree.await();
    const operations = presetOperations(ctx);
    for (const action of ['list', 'read', 'save', 'create']) ctx.effect(() => ctx.connection.fetch.register({
      path: '/api/dsh-agent-preset-editor/' + action, methods: ['POST'], requestBody: 'buffered',
      fetch: async request => {
        try {
          const body = await request.text();
          if (body.length > 3 * 1024 * 1024) throw new Error('Preset fields are too large');
          const args = body ? JSON.parse(body) : {};
          const result = await operations[action](action === 'read' ? args.id : args);
          return Response.json(result);
        } catch (error) { return Response.json({ error: error.message }, { status: 400 }); }
      },
    }), 'persona-editor: ' + action);
    ctx.inject(['settings'], scope => scope.effect(() => scope.settings.configure({ auto: false }, ctx.fiber)));
    ctx.inject(['tools', 'sandboxPolicy'], scope => scope.effect(() => scope.tools.register(defineTool({
      name: 'dsh_persona_editor',
      description: 'Read, edit or create Agent presets in the current DSH profile. Preset IDs stay fixed. Mutations require Full access or approval and only affect newly mounted Agents. Use list/read first; save uses the returned revision and create uses templateRevision.',
      parameters: {
        action: { type: 'string', required: true, enum: ['list', 'read', 'save', 'create'] },
        id: { type: 'string' }, name: { type: 'string' }, description: { type: 'string' }, order: { oneOf: [{ type: 'number' }, { type: 'string' }] },
        prefix: { type: 'string' }, suffix: { type: 'string' }, revision: { type: 'string' },
        personaPath: { type: 'array', items: { oneOf: [{ type: 'integer' }, { type: 'string' }] } },
        template: { type: 'string' }, templateRevision: { type: 'string' },
      },
      output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
      async execute(args, exec) {
        if (['save', 'create'].includes(args.action)) {
          const policy = scope.sandboxPolicy.resolve(exec.agent === undefined ? {} : { session: exec.agent.session });
          await approveEscalation({ requestedMode: 'danger-full-access', effectiveMode: policy.mode,
            subject: 'Agent preset configuration', justification: 'Save an Agent preset through DSH configEditor; changes persist across conversations.' },
          { approver: scope.get('approval'), agent: exec.agent, callId: exec.callId, toolName: 'dsh_persona_editor', signal: exec.signal });
          exec.signal.throwIfAborted();
        }
        return JSON.stringify(await operations[args.action](args.action === 'read' ? args.id : args));
      },
    })), 'persona-editor: shared Agent tool'));
  }
}
