const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function studio() {
  const notifications = [];
  const elements = new Map();
  const storage = new Map();
  const context = {
    window: { config: { get: () => 'local-model' },
      app: { showNotification: (...args) => notifications.push(args) } },
    console,
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, { value: '5', style: {} });
      return elements.get(id);
    } },
    localStorage: { setItem: (key, value) => storage.set(key, value) },
    CharacterGeneratorApp: function () {},
  };
  context.CharacterGeneratorApp.prototype.init = async () => {};
  vm.createContext(context);
  for (const file of ['api.js', 'api-lorebook.js', 'world-lorebook.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,
      '../src/engrai_server/cardgen/web/src/scripts', file), 'utf8'), context);
  }
  return { api: context.window.apiHandler, context, notifications, elements, storage };
}

const reply = (content, finish_reason = 'stop', reasoning_content = '') => ({
  choices: [{ message: { content, reasoning_content }, finish_reason }],
});
const book = { name: 'The Coast', setting: 'Tidal towns.', entries: [] };
const topic = { title: 'Saltmarket', keys: ['Saltmarket', 'fish market'] };
const plain = (value) => JSON.parse(JSON.stringify(value));

test('world planner constrains the schema and validates topics from permissive engines', async () => {
  const { api } = studio();
  let sent;
  api.makeRequest = async (_route, data) => {
    sent = data;
    return reply(JSON.stringify({ topics: [
      topic, { title: 'saltmarket', keys: ['duplicate'] },
      { title: {}, keys: ['bad'] }, { title: 'x'.repeat(121) },
      { title: 'Bells', keys: [null, {}, ' tide bells ', 'tide bells'] },
    ] }));
  };
  const result = await api.suggestWorldTopics(book, 3);
  assert.deepEqual(plain(result), [topic, { title: 'Bells', keys: ['tide bells'] }]);
  assert.equal(sent.response_format.type, 'json_object');
  const schema = sent.response_format.schema;
  assert.equal(schema.additionalProperties, false);
  assert.equal(schema.properties.topics.maxItems, 3);
  assert.equal(schema.properties.topics.items.properties.title.maxLength, 120);
  assert.equal(schema.properties.topics.items.properties.keys.maxItems, 4);
  assert.equal(sent.chat_template_kwargs.enable_thinking, false);
  assert.ok(sent.max_tokens >= 1500);
});

test('recovers complete topics before the repeating, unterminated string in the screenshot', async () => {
  const { api, notifications } = studio();
  api.makeRequest = async () => reply(
    '{"topics":[' + JSON.stringify(topic) + ',{"title":"title ' + 'own '.repeat(300), 'length');
  assert.deepEqual(plain(await api.suggestWorldTopics(book, 5)), [topic]);
  assert.equal(notifications.length, 1);
  assert.match(notifications[0][0], /complete topics/);
});

test('recovery respects escaped quotes, brackets, commas, and incomplete keys', () => {
  const { api } = studio();
  const unusual = { title: 'The "[Bell]," Guild', keys: ['bell\\guild', 'bell guild'] };
  const output = '<think>{"topics":["wrong"]}</think>```json\n{"topics":[' +
    JSON.stringify(unusual) + ',{"title":"unfinished","keys":["partial"';
  assert.deepEqual(plain(api._parseLorebookTopics(output)), [unusual]);
  assert.deepEqual(plain(api._parseLorebookTopics('{"topics":[' + JSON.stringify(unusual))), [unusual]);
  assert.deepEqual(plain(api._parseLorebookTopics('{"topics":[{"title":"broken')), []);
  assert.deepEqual(plain(api._parseLorebookTopics('<think>{"topics":["example"]}')), []);
});

test('accepts fenced JSON and legacy arrays; does not treat reasoning as the answer', async () => {
  const { api } = studio();
  for (const output of [JSON.stringify([topic]), '```json\n' + JSON.stringify({ topics: [topic] }) + '\n```']) {
    api.makeRequest = async () => reply(output);
    assert.deepEqual(plain(await api.suggestWorldTopics(book)), [topic]);
  }
  api.makeRequest = async () => reply('', 'length', JSON.stringify({ topics: [topic] }));
  await assert.rejects(api.suggestWorldTopics(book), /output token limit/);
  api.makeRequest = async () => reply('{"topics":[{"title":"unfinished', 'length');
  await assert.rejects(api.suggestWorldTopics(book), /output token limit/);
  api.makeRequest = async () => reply('{"unrelated":["not topics"]}');
  await assert.rejects(api.suggestWorldTopics(book), /usable topic list/);
});

test('character topic prompt agrees with object schema and returns only topic names', async () => {
  const { api } = studio();
  let sent;
  api.makeRequest = async (_route, data) => {
    sent = data;
    return reply('{"topics":["Saltmarket",null,{},"Bells","Saltmarket"]}');
  };
  assert.deepEqual(plain(await api.suggestLorebookTopics({ name: 'Ash' })), ['Saltmarket', 'Bells']);
  assert.equal(sent.response_format.schema.properties.topics.items.type, 'string');
  assert.match(sent.messages[0].content, /JSON object containing a "topics" array/);
  assert.equal(sent.chat_template_kwargs.enable_thinking, false);
});

test('Draft entries writes and persists topics recovered from truncated planning', async () => {
  const { api, context, elements, storage } = studio();
  const app = new context.CharacterGeneratorApp();
  app.worldbook = plain(book);
  app._worldbookSuggestions = [];
  app.apiHandler = api;
  app._requireText = app._requireSetting = () => true;
  app.renderWorldbookEntries = app.renderWorldbookSuggestions = () => {};
  app.showNotification = (message) => assert.fail(message);
  let writes = 0;
  api.makeRequest = async (_route, data) => {
    if (!data.stream) return reply('{"topics":[' + JSON.stringify(topic) + ',{"title":"own own', 'length');
    writes++;
    assert.match(data.messages[1].content, /Write the entry for: Saltmarket/);
    return {};
  };
  api.handleStreamResponse = async () => 'Saltmarket is the coastal fish market.';
  await app.draftWorldbookEntries();
  assert.equal(writes, 1);
  assert.equal(app.worldbook.entries[0].content, 'Saltmarket is the coastal fish market.');
  assert.equal(JSON.parse(storage.get('engrai.cardstudio.worldbook')).entries.length, 1);
  assert.match(elements.get('wb-progress').textContent, /Drafted 1 entry/);
});
