const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scripts = path.join(__dirname, '../src/engrai_server/cardgen/web/src/scripts');
const plain = value => JSON.parse(JSON.stringify(value));

function studio(card) {
  const notifications = [];
  const errors = [];
  const elements = new Map([
    'character-generated-name', 'character-description', 'character-personality',
    'character-scenario', 'character-first-message', 'image-controls',
    'image-buttons-row', 'image-content', 'download-btn', 'download-json-btn',
    'save-card-btn', 'input-section-details',
  ].map(id => [id, { value: '', style: {}, innerHTML: '' }]));
  const resultSection = { style: {}, scrollIntoView() {} };
  const context = vm.createContext({
    window: {}, Blob, URL, fetch,
    console: { ...console, error: (...args) => errors.push(args) },
    CharacterGeneratorApp: function () {},
    document: {
      getElementById: id => elements.get(id) || null,
      querySelector: selector => selector === '.result-section' ? resultSection : null,
    },
    authFetch: async url => {
      assert.equal(url, `/cardgen/api/storage/cards/${card.id}`);
      return { ok: true, json: async () => structuredClone(card) };
    },
  });
  for (const file of ['storage.js', 'app-ui.js', 'character-display.js',
    'image-handler.js', 'lorebook-handler.js', 'alt-greetings-handler.js', 'library-handler.js']) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), 'utf8'), context);
  }
  const app = new context.CharacterGeneratorApp();
  Object.assign(app, {
    storage: context.window.characterStorage,
    lorebookEntries: [], altGreetings: [], imageHistoryUrls: [],
    showNotification: (...args) => notifications.push(args),
  });
  const load = () => app.handleLibraryCardClick({ target: {
    closest: () => ({ dataset: { action: 'load-card', id: String(card.id) } }),
  } });
  return { app, load, elements, resultSection, notifications, errors };
}

for (const isPermanent of [true, false]) {
  test(`loads a ${isPermanent ? 'saved' : 'history'} character without SillyTavern integration`, async () => {
    const card = {
      id: 42, isPermanent,
      character: {
        name: 'Ash', description: 'A clockmaker.', firstMessage: 'Come in.',
        alternateGreetings: ['Welcome back.'],
        character_book: { entries: [{ id: 'town', keys: ['Town'], content: 'A coastal town.', enabled: true }] },
      },
      imageBase64: 'data:image/png;base64,aW1hZ2U=',
      imageHistory: ['data:image/png;base64,aGlzdG9yeQ=='],
    };
    const { app, load, elements, resultSection, notifications, errors } = studio(card);
    assert.equal(app._updatePushButton, undefined);
    await load();
    assert.deepEqual(errors, []);
    assert.deepEqual(notifications, [['Card loaded', 'success']]);
    assert.deepEqual(plain(app.currentCharacter), card.character);
    assert.deepEqual(plain(app.originalCharacter), card.character);
    assert.notEqual(app.originalCharacter, app.currentCharacter);
    assert.equal(app.currentCardId, '42');
    assert.equal(elements.get('character-generated-name').value, 'Ash');
    assert.equal(elements.get('character-description').value, 'A clockmaker.');
    assert.equal(resultSection.style.display, 'block');
    assert.equal(elements.get('save-card-btn').style.display, 'inline-flex');
    assert.deepEqual(plain(app.lorebookEntries), card.character.character_book.entries);
    assert.equal(app.altGreetings[0].content, 'Welcome back.');
    assert.match(app.currentImageUrl, /^blob:/);
    assert.ok(elements.get('image-content').innerHTML.includes(app.currentImageUrl));
    assert.deepEqual(plain(app.imageHistoryUrls), card.imageHistory);

    // Loading a text-only card must clear the previous card's media and extras.
    card.character = { name: 'Elm' };
    delete card.imageBase64;
    delete card.imageHistory;
    await load();
    assert.deepEqual(errors, []);
    assert.equal(elements.get('character-generated-name').value, 'Elm');
    assert.equal(app.currentImageUrl, null);
    assert.equal(app.imageHistoryUrls.length, 0);
    assert.equal(app.lorebookEntries.length, 0);
    assert.equal(app.altGreetings.length, 0);
    assert.match(elements.get('image-content').innerHTML, /No image/);
  });
}
