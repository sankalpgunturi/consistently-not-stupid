import { DurableObject } from 'cloudflare:workers';

const headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'};
const json = (body, status = 200) => Response.json(body, {status, headers});
const actions = new Set(['pause', 'scan', 'reset', 'block', 'knob', 'close', 'live']);

export class DeskSnapshot extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.csrf = await ctx.storage.get('csrf') || crypto.randomUUID();
      await ctx.storage.put('csrf', this.csrf);
    });
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === '/snapshot' && request.method === 'PUT') {
      await this.ctx.storage.put('snapshot', {state: await request.json(), received_at: new Date().toISOString()});
      return json({ok: true});
    }
    if (path === '/commands' && request.method === 'GET') {
      const queued = await this.ctx.storage.get('commands') || [];
      const fresh = queued.filter(c => Date.now() - c.created_at < 60000);
      await this.ctx.storage.put('commands', fresh);
      return json({commands: fresh});
    }
    if (path === '/ack') {
      const result = await request.json();
      const queued = await this.ctx.storage.get('commands') || [];
      await this.ctx.storage.put('commands', queued.filter(c => c.id !== result.id));
      await this.ctx.storage.put('last_command', result);
      return json({ok: true});
    }
    if (path.startsWith('/api/')) {
      if (request.headers.get('X-CSRF-Token') !== this.csrf) return json({error: 'Missing or bad CSRF token'}, 403);
      const saved = await this.ctx.storage.get('snapshot');
      if (!saved || Date.now() - Date.parse(saved.received_at) > 30000)
        return json({error: 'The paper runner is offline. Try again when updates resume.'}, 503);
      const queued = await this.ctx.storage.get('commands') || [];
      if (queued.length >= 20) return json({error: 'Commands are still pending. Try again shortly.'}, 429);
      let body = {};
      try { const text = await request.text(); if (text) body = JSON.parse(text); }
      catch { return json({error: 'Invalid JSON'}, 400); }
      const command = {id: crypto.randomUUID(), action: path.slice(5), body, created_at: Date.now()};
      await this.ctx.storage.put('commands', [...queued, command]);
      return json({status: 'pending', id: command.id}, 202);
    }
    const saved = await this.ctx.storage.get('snapshot');
    if (!saved) return json({csrf: this.csrf, status: 'stale', errors: ['Waiting for account updates.']});
    const stale = Date.now() - Date.parse(saved.received_at) > 30000;
    return json({...saved.state, csrf: this.csrf, published_at: saved.received_at,
      last_command: await this.ctx.storage.get('last_command'),
      status: stale ? 'stale' : saved.state.status,
      errors: stale ? ['Updates paused. Showing the last received account state.'] : saved.state.errors});
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin');
    if (origin && origin !== url.origin) return json({error: 'Foreign origin'}, 403);
    const desk = () => env.DESK.get(env.DESK.idFromName('paper'));
    if (url.pathname.startsWith('/internal/')) {
      if (origin || !env.PUBLISH_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.PUBLISH_TOKEN}`)
        return json({error: 'Unauthorized'}, 401);
      if (url.pathname === '/internal/commands' && request.method === 'GET')
        return desk().fetch(new Request('https://desk/commands'));
      if (url.pathname === '/internal/ack' && request.method === 'POST')
        return desk().fetch(new Request('https://desk/ack', {method: 'POST', body: await request.text()}));
      if (url.pathname !== '/internal/snapshot' || request.method !== 'POST') return json({error: 'Not found'}, 404);
      if (Number(request.headers.get('content-length')) > 2000000) return json({error: 'Too large'}, 413);
      const text = await request.text();
      if (text.length > 2000000) return json({error: 'Too large'}, 413);
      let body;
      try { body = JSON.parse(text); } catch { return json({error: 'Invalid JSON'}, 400); }
      if (!body || typeof body !== 'object' || !body.book || !Array.isArray(body.trades))
        return json({error: 'Invalid account snapshot'}, 400);
      delete body.csrf;
      return desk().fetch(new Request('https://desk/snapshot', {method: 'PUT', body: JSON.stringify(body)}));
    }
    if (url.pathname === '/api/state' && request.method === 'GET')
      return desk().fetch(new Request('https://desk/snapshot'));
    if (request.method === 'POST' && actions.has(url.pathname.slice(5)) && url.pathname.startsWith('/api/')) {
      if (Number(request.headers.get('content-length')) > 4096) return json({error: 'Too large'}, 413);
      const body = await request.text();
      if (body.length > 4096) return json({error: 'Too large'}, 413);
      return desk().fetch(new Request('https://desk' + url.pathname, {method: 'POST', headers: request.headers, body}));
    }
    if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return json({error: 'Not found'}, 404);
    return env.ASSETS.fetch(request);
  }
};
