import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

test('only real CLI children drive working poses; team reservations wait for handoff', async () => {
  const context = vm.createContext({});
  const source = await readFile(new URL('../web/office-scene.js', import.meta.url), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link(specifier => new vm.SyntheticModule(
    specifier === 'three' ? [] : ['OrbitControls'], function () {
      if (specifier !== 'three') this.setExport('OrbitControls', class {});
    }, { context }));
  await module.evaluate();
  const scene = Object.create(module.namespace.OfficeScene.prototype);
  scene._disposed = false;
  scene._rooms = new Map(['codex', 'antigravity'].map(id => [id, { id, screen: {}, avatar: {} }]));
  scene._labels = new Map(['codex', 'antigravity'].map(id => [id, {
    name: {}, model: {}, status: { style: {} }, button: { setAttribute() {} },
  }]));
  scene._resetPose = () => {};
  scene._updateLabels = () => {};
  const providers = ['codex', 'antigravity'].map(id => ({
    id, available: true, team_run_id: 'team', active_job_id: id === 'codex' ? 'team' : 'child',
  }));
  const parent = { id: 'team', kind: 'collaboration', provider: 'codex', status: 'running' };
  const child = { id: 'child', kind: 'delegation', parent_job_id: 'team', provider: 'antigravity', status: 'running' };
  scene.applyState({ providers, jobs: [parent, child] });
  assert.equal(scene._rooms.get('codex').phase, 'waiting');
  assert.equal(scene._rooms.get('codex').job, null);
  assert.equal(scene._rooms.get('antigravity').phase, 'running');
  child.status = 'completed';
  scene.applyState({ providers, jobs: [parent, child] });
  assert.equal(scene._rooms.get('antigravity').phase, 'waiting');
  scene.applyState({ providers, jobs: [parent, child], view_connected: false });
  assert.equal(scene._rooms.get('codex').phase, 'offline');
});
