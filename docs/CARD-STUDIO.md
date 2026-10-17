# Card Studio

Card Studio is the character-card authoring app served by the gateway at `/cardgen/`. It writes Character Card V2 characters, groups, and scenarios, builds lorebooks, alternate greetings, and example dialogue, and draws, edits, enhances, and combines portraits. Cards export as V2 JSON or as PNG with the card embedded, which SillyTavern and similar clients import.

It is adapted from CardGen V2 (MIT); see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

## Signing in

There is no separate account. Open **Card Studio** from the console's top bar. It uses the console's session, which lives in that browser tab: open it in the same tab, not a new one. Visiting `/cardgen/` with no session sends you to the console's lock screen, which returns you to Card Studio after you sign in.

## Models
There is no model or provider sheet. Card Studio writes with whichever text model the console has loaded and draws with whichever image model it has loaded, and the top bar shows both. If the one it needs is not loaded, it says so instead of loading one itself. Load or switch models in the console, and Card Studio follows within a few seconds.

Every image job runs on the image worker (stable-diffusion.cpp):

| Job | How it runs |
| --- | --- |
| Generate | text-to-image |
| Edit | image-to-image from the source (strength 0.8), with the source also passed as a reference image |
| Enhance | image-to-image at the larger size (strength 0.35), capped at 2048 px per side |
| Combine | text-to-image with every input as a reference image |

Reference images matter only to architectures that read them, such as FLUX.2, Kontext, and Qwen-Image Edit. Sizes are snapped to the worker's 64 px grid.

## Lorebooks
There are two kinds, and they stay separate.

- **A character's lorebook** lives on its card: Manage Lorebook on the Characters tab. It is embedded in the card when you export it.
- **A world lorebook** stands alone, on the Lorebooks tab: a setting, city, faction or system of magic that any character can use. Describe the setting once; then suggest topics, draft several entries at a time, or write and revise entries one by one. Every entry is written to agree with the setting.

World lorebooks download in SillyTavern's World Info format, and import from World Info files, from a character_book, or from the lorebook inside a card (`.json` or `.png`). **Add to current character** copies a world lorebook's entries into the open character's own lorebook. It is a copy: editing either afterward does not change the other. Unsaved work on the tab is kept in the browser until you save it to the library.

## Library and backup
Cards, prompts, galleries, world lorebooks and Card Studio's own settings are stored in `${XDG_DATA_HOME:-~/.local/share}/engrai-server/cardgen/` (`ENGRAI_CARDGEN_DATA_DIR` overrides it). Each kind is one JSON file, rewritten atomically. Back up the whole directory. A CardGen V2 user directory (`proxy/data/users/<id>/`) uses the same layout and can be copied in as-is.

## Phone resilience

Generations started from Card Studio run as server-side jobs. If a phone locks mid-generation, the engine finishes into a buffer, and the page resumes the stream or collects the result when it comes back. Jobs are kept for ten minutes in memory and do not survive a gateway restart.

## Not carried over from CardGen
CardGen features that depended on outside services are not part of Card Studio, because the gateway makes no outbound requests on a page's behalf: pushing to and browsing a SillyTavern server, web search for characters and portraits, importing from character-site URLs, and Pollinations, Forge or ComfyUI image backends. Export a PNG or JSON and import it where you need it.
