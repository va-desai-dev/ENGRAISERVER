// One export boundary for JSON, PNG, and local persistence.
(function (root) {
  const string = (value) => typeof value === 'string' ? value : '';
  const strings = (value) => Array.isArray(value) ? value.filter(v => typeof v === 'string') : [];
  const object = (value) => value && typeof value === 'object' && !Array.isArray(value);
  function toV2(character) {
    const c = character?.data || character || {};
    const data = {
      name: string(c.name), description: string(c.description), personality: string(c.personality),
      scenario: string(c.scenario), first_mes: string(c.firstMessage ?? c.first_mes),
      mes_example: string(c.mesExample ?? c.mes_example),
      creator_notes: string(c.creatorNotes ?? c.creator_notes),
      system_prompt: string(c.systemPrompt ?? c.system_prompt),
      post_history_instructions: string(c.postHistoryInstructions ?? c.post_history_instructions),
      alternate_greetings: strings(c.alternateGreetings ?? c.alternate_greetings),
      tags: strings(c.tags), creator: string(c.creator),
      character_version: string(c.characterVersion ?? c.character_version),
      extensions: object(c.extensions) ? structuredClone(c.extensions) : {},
    };
    if (!data.name.trim()) throw new Error('Give the character a name before saving or exporting.');
    if (object(c.character_book)) data.character_book = structuredClone(c.character_book);
    return { spec: 'chara_card_v2', spec_version: '2.0', data };
  }
  root.CardV2 = { toV2 };
  if (typeof module !== 'undefined') module.exports = root.CardV2;
})(typeof window !== 'undefined' ? window : globalThis);
