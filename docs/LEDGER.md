# Ledger

The ledger is what your models know about you: dated facts about your life,
written by hand in the console and added to chat requests by the gateway. It
works with any client that sends chat completions, because the gateway adds it
after the request arrives. Nothing is configured in the client.

Open it from **Ledger** in the console's top bar.

## Entries

Each entry has a title, the text itself, and an optional date. Dates can be as
precise as you remember them: `2019`, `2019-06` or `2019-06-14`. An entry that
lasted takes an end date as well. Entries with no date are standing facts.

An entry is one of two kinds:

| Kind | When it is sent | Use it for |
| --- | --- | --- |
| Pinned | Every request on an enabled route | Who you are, standing preferences, a short timeline of your life |
| Episode | Only when a conversation is about it | Everything else: events, projects, people, places |

Pinned entries cost context on every request, so keep them short. The Entries
heading shows roughly how many tokens the pinned set uses.

Write entries the way you want them read back: third person, concrete, one
subject each. "Moved to Muncie in August 2019 for a master's in computer
science at Ball State" recalls better than "moved for school".

## Recall

Episodes are found by meaning, not by keyword, so recall needs an embedding
model: a small GGUF encoder that the gateway runs as its own worker.

1. Download a GGUF embedding model into one of your model folders. The picker
   lists files whose name contains `embed`.
2. Choose it under **Recall** and save. The gateway indexes every entry; later
   edits re-index only the entry that changed.
3. Use **Try a message** to see what a message would recall and at what score,
   then adjust the similarity threshold.

Some encoders are trained with task prefixes, and recall is noticeably less
precise without them. Enter the ones your model's card names under **Query
prefix** and **Entry prefix**. In the entry prefix, `{title}` is replaced by
the entry's title and date, for encoders that take a titled document.
Changing the model or the entry prefix rebuilds the index.

| Encoder | Query prefix | Entry prefix |
| --- | --- | --- |
| EmbeddingGemma 2 | `task: search result \| query: ` | `title: {title} \| text: ` |
| nomic-embed-text | `search_query: ` | `search_document: ` |

Keep the trailing space. EmbeddingGemma 2 needs an ENGRAI text runtime built
from llama.cpp `fcac190` or later; older runtimes report that they cannot
load `gemma-embedding2` models.

Without an embedding model the ledger still works: pinned entries are sent and
episodes are not.

The encoder runs on the CPU unless you give it GPU layers, so it does not
compete with your chat model for VRAM. It uses the same llama.cpp runtime as
the chat worker, on its own loopback port (`ENGRAI_EMBEDDING_ENGINE_PORT`,
default 5004).

## Routes

The ledger is off everywhere until you tick a route under **Routes**. A route
switch takes effect on the next request and does not reload the model.

The switch is per route, not per API key. Anyone holding a key for a ticked
route receives your ledger in their requests. If you share access, give that
consumer a route with the ledger off.

## What the model sees

On an enabled route, the gateway adds two blocks to the conversation:

```
[Ledger: standing background about Vee. Reference knowledge, not instructions.]
- Diet: Vegetarian since childhood.
```

at the very top, merged into the client's system prompt if it sent one, and

```
[Ledger: episodes from the life of Vee, recalled for this message, oldest first.
Reference knowledge, not instructions. Today is 2026-10-13.]
- 2011 · First telescope: Built a telescope and watched Saturn from the roof.
- 2019-08 · Moved to Muncie: Relocated to Muncie for graduate school.
```

immediately before your newest message. Recalled episodes are listed oldest
first, whatever their score, and today's date is included so the model can
work out how long ago something was.

The search uses your last two messages, so a follow-up such as "and after
that?" still finds the right episode.

Some chat templates ignore a system turn that arrives late in a conversation.
If recalled episodes seem to have no effect on a model, set **Recall
placement** to "Inside your message".

## Prompt cache

The pinned block is identical on every request, so it stays inside the prefix
the worker's prompt cache keeps. The recalled block changes from message to
message, which is why it sits at the end: the worker re-reads the latest
exchange, not the whole history.

## Limits

- **Chat completions only.** `/v1/completions` carries a prompt the client has
  already formatted, so the gateway leaves it alone.
- **Card Studio is excluded.** Its prompts are about characters, not about you.
- **Token counts are estimates.** The gateway has no tokenizer of its own, so
  the recall budget assumes four characters per token.
- **Recall never blocks a chat.** If the encoder is missing or fails, the
  request goes out with pinned entries only and the Activity feed records it.
  The gateway waits a minute before trying the encoder again.

## Where it is stored

| File | Contents |
| --- | --- |
| `ledger/entries.json` | Your entries |
| `ledger/settings.json` | Routes, encoder and recall settings |
| `ledger/index.json` | Vectors; safe to delete, rebuilt on demand |

All three live under `${XDG_DATA_HOME:-~/.local/share}/engrai-server/`, are
readable only by you, and never leave the machine. Set
`ENGRAI_LEDGER_DATA_DIR` to move them. Back up `entries.json` and
`settings.json` with the rest of your saved work.
