const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('character generation preserves auth failures without retrying removed provider auth', async () => {
  const context = vm.createContext({
    window: { config: { get: () => 'local-model', log() {} } },
    console,
  });
  for (const file of ['api.js', 'api-character.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,
      '../src/engrai_server/cardgen/web/src/scripts', file), 'utf8'), context);
  }
  const api = context.window.apiHandler;
  api.buildCharacterPrompt = () => ({ systemPrompt: '', userPrompt: '' });

  for (const message of ['401 Unauthorized', 'Authorization failed']) {
    const failure = new Error(message);
    let calls = 0;
    api.makeRequest = async () => {
      calls++;
      throw failure;
    };
    await assert.rejects(api.generateCharacter('A clockmaker', 'Ash'), error => error === failure);
    assert.equal(calls, 1);
  }
});
